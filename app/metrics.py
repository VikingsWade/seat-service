from __future__ import annotations

import asyncio

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

from .db import Database

DECLINE_REASONS = (
    "seat_taken",
    "per_user_limit",
    "idempotent_replay",
    "idempotency_key_conflict",
    "unknown_seat",
)

_SEAT_COUNTS_SQL = """
SELECT sh.id::text AS show_id,
       count(*) FILTER (WHERE st.status = 'available') AS available,
       count(*) FILTER (WHERE st.status = 'held')      AS held,
       count(*) FILTER (WHERE st.status = 'confirmed') AS confirmed
FROM (SELECT id FROM shows ORDER BY created_at DESC LIMIT $1) sh
JOIN seats st ON st.show_id = sh.id
GROUP BY sh.id
"""


class Metrics:
    content_type = CONTENT_TYPE_LATEST

    def __init__(self, max_shows: int = 20) -> None:
        self._max_shows = max_shows
        self._lock = asyncio.Lock()
        self.registry = CollectorRegistry()
        r = self.registry

        self.confirmed = Counter(
            "reservations_confirmed", "Reservations confirmed since process start.", registry=r
        )
        self.seats_confirmed = Counter(
            "seats_confirmed", "Seats confirmed since process start.", registry=r
        )
        self.declined = Counter(
            "reservations_declined",
            "Reservation requests declined or replayed, by reason.",
            ["reason"],
            registry=r,
        )
        self.cancelled = Counter(
            "reservations_cancelled", "Reservations cancelled since process start.", registry=r
        )
        for reason in DECLINE_REASONS:
            self.declined.labels(reason)

        self.seats_available = Gauge(
            "seats_available", "Seats currently available, read from the database at scrape time.", ["show_id"], registry=r
        )
        self.seats_held = Gauge(
            "seats_held", "Seats currently held, read from the database at scrape time.", ["show_id"], registry=r
        )
        self.seats_confirmed_now = Gauge(
            "seats_confirmed_current", "Seats currently confirmed, read from the database at scrape time.", ["show_id"], registry=r
        )
        self.database_up = Gauge("database_up", "1 if the last database probe succeeded.", registry=r)

        self.http_requests = Counter(
            "http_requests", "HTTP requests by method, route and status.", ["method", "route", "status"], registry=r
        )
        self.http_latency = Histogram(
            "http_request_duration_seconds",
            "HTTP request latency.",
            ["method", "route"],
            buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30),
            registry=r,
        )
        self.in_flight = Gauge("http_requests_in_flight", "Requests currently being served.", registry=r)

    async def _refresh_seat_gauges(self, db: Database) -> None:
        try:
            rows = await db.monitor_fetch(_SEAT_COUNTS_SQL, self._max_shows)
        except Exception:
            self.database_up.set(0)
            return
        self.database_up.set(1)
        for gauge in (self.seats_available, self.seats_held, self.seats_confirmed_now):
            gauge.clear()
        for row in rows:
            self.seats_available.labels(row["show_id"]).set(row["available"])
            self.seats_held.labels(row["show_id"]).set(row["held"])
            self.seats_confirmed_now.labels(row["show_id"]).set(row["confirmed"])

    async def render(self, db: Database) -> bytes:
        async with self._lock:
            await self._refresh_seat_gauges(db)
            return generate_latest(self.registry)
