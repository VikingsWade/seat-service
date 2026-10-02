from __future__ import annotations

import asyncio
import hashlib
import json
import random
import uuid
from typing import Any, NamedTuple, Optional

import asyncpg

from .config import Settings
from .db import Database
from .errors import ApiError, Decline, RestartTransaction
from .metrics import Metrics

_RETRYABLE = (asyncpg.exceptions.TransactionRollbackError, RestartTransaction)


class ReserveOutcome(NamedTuple):
    status: int
    body: dict
    replay: bool


def request_hash(show_id: uuid.UUID, seats: list[str]) -> str:
    canonical = json.dumps({"show": str(show_id), "seats": sorted(seats)}, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _rows_affected(command_tag: str) -> int:
    return int(command_tag.split()[-1])


def _reservation_body(row: Any, status: str) -> dict:
    return {
        "reservation_id": str(row["id"]),
        "show_id": str(row["show_id"]),
        "user_id": row["user_id"],
        "seats": list(row["seats"]),
        "amount_paise": row["amount_paise"],
        "status": status,
    }


class SeatService:
    def __init__(self, db: Database, metrics: Metrics, settings: Settings) -> None:
        self.db = db
        self.metrics = metrics
        self.settings = settings

    async def _in_tx(self, fn, *args, attempts: int = 5):
        """Run `fn(conn, *args)` in one transaction, retrying deadlocks and restarts."""
        for attempt in range(1, attempts + 1):
            try:
                async with self.db.connection() as conn:
                    async with conn.transaction():
                        return await fn(conn, *args)
            except _RETRYABLE:
                if attempt == attempts:
                    raise
                await asyncio.sleep(random.uniform(0.002, 0.02) * attempt)

    # ------------------------------------------------------------------ shows

    async def create_show(self, name: str, seats: list[str], price_paise: int, per_user_limit: int) -> dict:
        show_id = uuid.uuid4()
        async with self.db.connection() as conn:
            async with conn.transaction():
                created_at = await conn.fetchval(
                    "INSERT INTO shows (id, name, price_paise, per_user_limit, total_seats) "
                    "VALUES ($1, $2, $3, $4, $5) RETURNING created_at",
                    show_id, name, price_paise, per_user_limit, len(seats),
                )
                await conn.execute(
                    "INSERT INTO seats (show_id, label) SELECT $1::uuid, unnest($2::text[])",
                    show_id, seats,
                )
        total = len(seats)
        return {
            "id": str(show_id),
            "name": name,
            "price_paise": price_paise,
            "per_user_limit": per_user_limit,
            "total_seats": total,
            "available": total,
            "held": 0,
            "confirmed": 0,
            "counts": {"available": total, "held": 0, "confirmed": 0, "total": total},
            "seats": {label: "available" for label in seats},
            "created_at": created_at.isoformat(),
        }

    async def get_show(self, show_id: uuid.UUID, include_seats: bool = True) -> dict:
        async with self.db.connection() as conn:
            async with conn.transaction(isolation="repeatable_read", readonly=True):
                show = await conn.fetchrow(
                    "SELECT id, name, price_paise, per_user_limit, total_seats, created_at "
                    "FROM shows WHERE id = $1",
                    show_id,
                )
                if show is None:
                    raise ApiError(404, "show_not_found", "show not found")
                grouped = await conn.fetch(
                    "SELECT status, count(*) AS n FROM seats WHERE show_id = $1 GROUP BY status", show_id
                )
                seat_rows = None
                if include_seats:
                    seat_rows = await conn.fetch(
                        "SELECT label, status FROM seats WHERE show_id = $1 ORDER BY label", show_id
                    )
        counts = {"available": 0, "held": 0, "confirmed": 0}
        for row in grouped:
            counts[row["status"]] = row["n"]
        total = show["total_seats"]
        body = {
            "id": str(show["id"]),
            "name": show["name"],
            "price_paise": show["price_paise"],
            "per_user_limit": show["per_user_limit"],
            "total_seats": total,
            "available": counts["available"],
            "held": counts["held"],
            "confirmed": counts["confirmed"],
            "counts": {**counts, "total": total},
            "invariant_ok": sum(counts.values()) == total,
            "created_at": show["created_at"].isoformat(),
        }
        if seat_rows is not None:
            body["seats"] = {row["label"]: row["status"] for row in seat_rows}
        return body

    # ---------------------------------------------------------------- reserve

    async def reserve(
        self, user_id: str, show_id: uuid.UUID, seats: list[str], idempotency_key: Optional[str]
    ) -> ReserveOutcome:
        digest = request_hash(show_id, seats)
        try:
            outcome = await self._in_tx(self._reserve_tx, user_id, show_id, seats, idempotency_key, digest)
        except Decline as decline:
            self.metrics.declined.labels(decline.reason).inc()
            raise
        if outcome.replay:
            self.metrics.declined.labels("idempotent_replay").inc()
        else:
            self.metrics.confirmed.inc()
            self.metrics.seats_confirmed.inc(len(seats))
        return outcome

    async def _reserve_tx(
        self,
        conn: asyncpg.Connection,
        user_id: str,
        show_id: uuid.UUID,
        seats: list[str],
        idempotency_key: Optional[str],
        digest: str,
    ) -> ReserveOutcome:
        # Serialise this user's requests for this show. Held until commit, so the
        # limit count and the idempotency lookup below can never interleave with
        # another request from the same user.
        await conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended($1::text, 0))", f"{user_id}|{show_id}"
        )

        if idempotency_key is not None:
            prior = await conn.fetchrow(
                "SELECT request_hash, response FROM idempotency_keys WHERE user_id = $1 AND key = $2",
                user_id, idempotency_key,
            )
            if prior is not None:
                if prior["request_hash"] != digest:
                    raise Decline(
                        409,
                        "idempotency_key_conflict",
                        "this idempotency key was already used with a different request",
                    )
                return ReserveOutcome(200, json.loads(prior["response"]), True)

        show = await conn.fetchrow(
            "SELECT sh.price_paise, sh.per_user_limit, "
            "       (SELECT count(*) FROM seats "
            "         WHERE show_id = sh.id AND user_id = $2 AND status IN ('held', 'confirmed')) AS owned "
            "FROM shows sh WHERE sh.id = $1",
            show_id, user_id,
        )
        if show is None:
            raise ApiError(404, "show_not_found", "show not found")

        limit, owned = show["per_user_limit"], show["owned"]
        if owned + len(seats) > limit:
            raise Decline(
                409,
                "per_user_limit",
                f"a user may hold at most {limit} seats for this show",
                limit=limit,
                held=owned,
                requested=len(seats),
            )

        # Cheap pre-check without row locks: during a stampede on one seat this lets
        # the losers decline immediately instead of queueing behind the winner.
        taken = await conn.fetch(
            "SELECT label FROM seats WHERE show_id = $1 AND label = ANY($2::text[]) "
            "AND status <> 'available' ORDER BY label",
            show_id, seats,
        )
        if taken:
            raise Decline(
                409, "seat_taken", "one or more requested seats are not available",
                seats=[row["label"] for row in taken],
            )

        # The atomic decision: lock every requested seat row in label order (no deadlock
        # between multi-seat requests), then re-read the state under the lock.
        locked = await conn.fetch(
            "SELECT label, status FROM seats WHERE show_id = $1 AND label = ANY($2::text[]) "
            "ORDER BY label FOR UPDATE",
            show_id, seats,
        )
        if len(locked) != len(seats):
            known = {row["label"] for row in locked}
            raise Decline(
                404, "unknown_seat", "one or more seats do not exist for this show",
                seats=[s for s in seats if s not in known],
            )
        unavailable = [row["label"] for row in locked if row["status"] != "available"]
        if unavailable:
            raise Decline(
                409, "seat_taken", "one or more requested seats are not available", seats=unavailable
            )

        reservation_id = uuid.uuid4()
        amount = show["price_paise"] * len(seats)
        await conn.execute(
            "INSERT INTO reservations (id, show_id, user_id, seats, amount_paise, status) "
            "VALUES ($1, $2, $3, $4, $5, 'confirmed')",
            reservation_id, show_id, user_id, seats, amount,
        )
        updated = await conn.execute(
            "UPDATE seats SET status = 'confirmed', user_id = $3, reservation_id = $4 "
            "WHERE show_id = $1 AND label = ANY($2::text[]) AND status = 'available'",
            show_id, seats, user_id, reservation_id,
        )
        if _rows_affected(updated) != len(seats):
            raise Decline(409, "seat_taken", "one or more requested seats are not available")

        body = {
            "reservation_id": str(reservation_id),
            "show_id": str(show_id),
            "user_id": user_id,
            "seats": seats,
            "amount_paise": amount,
            "status": "confirmed",
        }
        if idempotency_key is not None:
            stored = await conn.fetchval(
                "INSERT INTO idempotency_keys (user_id, key, request_hash, reservation_id, response) "
                "VALUES ($1, $2, $3, $4, $5::jsonb) ON CONFLICT DO NOTHING RETURNING 1",
                user_id, idempotency_key, digest, reservation_id, json.dumps(body),
            )
            if stored is None:
                # The same key committed for another show while we were working.
                raise RestartTransaction()
        return ReserveOutcome(201, body, False)

    # ----------------------------------------------------------------- cancel

    async def cancel(self, user_id: str, reservation_id: uuid.UUID) -> dict:
        body = await self._in_tx(self._cancel_tx, user_id, reservation_id)
        if body.pop("_changed"):
            self.metrics.cancelled.inc()
        return body

    async def _cancel_tx(self, conn: asyncpg.Connection, user_id: str, reservation_id: uuid.UUID) -> dict:
        row = await conn.fetchrow(
            "SELECT id, show_id, user_id, seats, amount_paise, status "
            "FROM reservations WHERE id = $1 FOR UPDATE",
            reservation_id,
        )
        if row is None:
            raise ApiError(404, "reservation_not_found", "reservation not found")
        if row["user_id"] != user_id:
            raise ApiError(403, "forbidden", "only the owner can cancel a reservation")
        if row["status"] == "cancelled":
            return {**_reservation_body(row, "cancelled"), "_changed": False}

        # Same lock order as reserve(): by seat label.
        await conn.fetch(
            "SELECT label FROM seats WHERE reservation_id = $1 ORDER BY label FOR UPDATE", reservation_id
        )
        # Guarded on reservation_id, so a seat that now belongs to someone else is untouched.
        await conn.execute(
            "UPDATE seats SET status = 'available', user_id = NULL, reservation_id = NULL "
            "WHERE reservation_id = $1",
            reservation_id,
        )
        await conn.execute(
            "UPDATE reservations SET status = 'cancelled', cancelled_at = now() WHERE id = $1",
            reservation_id,
        )
        return {**_reservation_body(row, "cancelled"), "_changed": True}

    async def get_reservation(self, user_id: str, reservation_id: uuid.UUID) -> dict:
        async with self.db.connection() as conn:
            row = await conn.fetchrow(
                "SELECT id, show_id, user_id, seats, amount_paise, status "
                "FROM reservations WHERE id = $1",
                reservation_id,
            )
        if row is None:
            raise ApiError(404, "reservation_not_found", "reservation not found")
        if row["user_id"] != user_id:
            raise ApiError(403, "forbidden", "not your reservation")
        return _reservation_body(row, row["status"])
