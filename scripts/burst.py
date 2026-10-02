#!/usr/bin/env python3
"""On-sale stampede against a running instance.

Phases
  1. hot-seat storm   many users, one seat: exactly one 201, everyone else a clean 409
  2. stampede         thousands of users over a hot/cold seat mix, multi-seat requests,
                      concurrent retries that reuse an idempotency key
  3. per-user limit   one user firing parallel reserves past the limit
  4. idempotency      replay, same key with different seats, same key fired in parallel
  5. identity/cancel  spoofed body user, cross-user cancel, cancel never resurrects a seat

It prints the outcome distribution, latency percentiles, the final reconciliation of every
show, and compares the service's Prometheus counters with what this client observed.
Exit status is non-zero if any invariant is violated or any 5xx / client error is seen.
"""
import argparse
import asyncio
import collections
import json
import os
import random
import sys
import time
import uuid
from urllib.parse import urlparse

import aiohttp

LIMIT = 4


class Result:
    __slots__ = ("status", "body", "text", "replay", "elapsed", "error")

    def __init__(self, status, body, text, replay, elapsed, error):
        self.status = status
        self.body = body
        self.text = text
        self.replay = replay
        self.elapsed = elapsed
        self.error = error

    @property
    def code(self):
        return self.body.get("error") if isinstance(self.body, dict) else None


class Api:
    def __init__(self, session, base):
        self.session = session
        self.base = base.rstrip("/")

    async def call(self, method, path, token=None, body=None):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        started = time.perf_counter()
        try:
            async with self.session.request(
                method, self.base + path, json=body, headers=headers
            ) as resp:
                text = await resp.text()
                try:
                    parsed = json.loads(text) if text else None
                except ValueError:
                    parsed = None
                return Result(
                    resp.status, parsed, text,
                    resp.headers.get("Idempotent-Replay") == "true",
                    time.perf_counter() - started, None,
                )
        except Exception as exc:  # timeouts, resets, refused connections
            return Result(0, None, "", False, time.perf_counter() - started, f"{type(exc).__name__}: {exc}")


def classify(r):
    if r.error:
        return "client_error"
    if r.status == 201:
        return "confirmed"
    if r.status == 200 and r.replay:
        return "idempotent_replay"
    if r.status == 409 and r.code:
        return f"declined:{r.code}"
    if r.status == 404 and r.code == "unknown_seat":
        return "declined:unknown_seat"
    if r.status >= 500:
        return "5xx"
    if 400 <= r.status < 500:
        return f"other_4xx:{r.status}"
    return f"other:{r.status}"


def percentile(sorted_values, p):
    if not sorted_values:
        return 0.0
    index = min(len(sorted_values) - 1, int(round(p / 100 * (len(sorted_values) - 1))))
    return sorted_values[index]


def parse_metrics(text):
    values = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        name, _, raw = line.rpartition(" ")
        try:
            values[name] = float(raw)
        except ValueError:
            pass
    return values


class Context:
    def __init__(self, api, args, admin_token):
        self.api = api
        self.args = args
        self.admin = admin_token
        self.run = uuid.uuid4().hex[:6]
        self.outcomes = collections.Counter()
        self.latencies = []
        self.violations = []
        self.recon_lines = []
        self.expected_cancelled = 0

    def fail(self, message):
        self.violations.append(message)
        print(f"  VIOLATION: {message}")

    def check(self, condition, message):
        if not condition:
            self.fail(message)

    def record(self, title, results, elapsed):
        tally = collections.Counter(classify(r) for r in results)
        self.outcomes.update(tally)
        self.latencies.extend(r.elapsed for r in results)
        lat = sorted(r.elapsed for r in results)
        rate = len(results) / elapsed if elapsed > 0 else 0.0
        print(f"\n== {title}")
        print(f"   {len(results)} requests in {elapsed:.2f}s ({rate:.0f} req/s)  "
              f"latency p50={percentile(lat, 50)*1000:.0f}ms p95={percentile(lat, 95)*1000:.0f}ms "
              f"p99={percentile(lat, 99)*1000:.0f}ms")
        for name, count in sorted(tally.items(), key=lambda kv: -kv[1]):
            print(f"   {name:<34}{count:>8}")
        return tally

    def record_calls(self, results):
        self.outcomes.update(classify(r) for r in results)
        self.latencies.extend(r.elapsed for r in results)


async def mint_tokens(api, users):
    async def one(user):
        r = await api.call("POST", "/auth/token", body={"user_id": user})
        if r.status != 200 or not r.body:
            raise RuntimeError(f"cannot mint a token for {user}: status={r.status} error={r.error or r.text[:200]}")
        return user, r.body["token"]

    return dict(await asyncio.gather(*(one(u) for u in users)))


async def create_show(ctx, name, seats, limit=LIMIT):
    r = await ctx.api.call(
        "POST", "/shows", ctx.admin,
        {"name": name, "seats": seats, "price_paise": 25000, "per_user_limit": limit},
    )
    if r.status != 201 or not r.body:
        raise RuntimeError(f"cannot create show: status={r.status} error={r.error or r.text[:300]}")
    return r.body["id"]


def reserve(ctx, show, token, seats, key=None, extra=None):
    body = {"seats": seats, "idempotency_key": key or uuid.uuid4().hex}
    if extra:
        body.update(extra)
    return ctx.api.call("POST", f"/shows/{show}/reserve", token, body)


async def reconcile(ctx, label, show):
    r = await ctx.api.call("GET", f"/shows/{show}")
    if r.status != 200 or not r.body:
        ctx.fail(f"{label}: cannot read show state (status={r.status})")
        return None
    s = r.body
    total = s["available"] + s["held"] + s["confirmed"]
    line = (f"{label}: available={s['available']} held={s['held']} confirmed={s['confirmed']} "
            f"total_seats={s['total_seats']} -> sum={total}")
    ctx.recon_lines.append(line)
    print(f"   reconcile  {line}")
    ctx.check(total == s["total_seats"], f"{label}: available+held+confirmed != total_seats")
    return s


# ---------------------------------------------------------------------- phases


async def phase_hot_seat(ctx):
    n = ctx.args.storm_users
    show = await create_show(ctx, f"hot-seat-{ctx.run}", [f"A{i}" for i in range(1, 51)])
    users = [f"hot-{ctx.run}-{i}" for i in range(n)]
    tokens = await mint_tokens(ctx.api, users)
    started = time.perf_counter()
    results = await asyncio.gather(*(reserve(ctx, show, tokens[u], ["A12"]) for u in users))
    ctx.record(f"1. hot-seat storm: {n} users, seat A12", results, time.perf_counter() - started)

    wins = [r for r in results if r.status == 201]
    ctx.check(len(wins) == 1, f"hot seat A12 had {len(wins)} winners, expected exactly 1")
    losers = [r for r in results if r.status != 201]
    ctx.check(all(r.status == 409 and r.code == "seat_taken" for r in losers),
              "every loser of the hot-seat storm must get a 409 seat_taken")
    state = await reconcile(ctx, "hot-seat show", show)
    if state:
        ctx.check(state["seats"].get("A12") == "confirmed" and state["confirmed"] == 1,
                  "after the storm exactly seat A12 must be confirmed")


async def phase_stampede(ctx):
    args = ctx.args
    n = args.seats
    rng = random.Random(args.seed)
    show = await create_show(ctx, f"stampede-{ctx.run}", [f"S{i}" for i in range(1, n + 1)])

    plans = []
    for i in range(args.users):
        idx = rng.randint(1, min(args.hot_seats, n)) if rng.random() < args.hot_fraction else rng.randint(1, n)
        wanted = [f"S{idx}"]
        if idx < n and rng.random() < 0.10:
            wanted.append(f"S{idx + 1}")
        plans.append({"user": f"u-{ctx.run}-{i}", "seats": wanted, "key": uuid.uuid4().hex,
                      "retry": rng.random() < 0.15})
    print(f"\n   minting {len(plans)} tokens ...")
    tokens = await mint_tokens(ctx.api, [p["user"] for p in plans])

    jobs = []
    for p in plans:
        jobs.append((p, reserve(ctx, show, tokens[p["user"]], p["seats"], p["key"])))
        if p["retry"]:
            jobs.append((p, reserve(ctx, show, tokens[p["user"]], p["seats"], p["key"])))
    rng.shuffle(jobs)

    started = time.perf_counter()
    results = await asyncio.gather(*(job for _, job in jobs))
    elapsed = time.perf_counter() - started
    tally = ctx.record(
        f"2. stampede: {len(plans)} users, {n} seats, {args.hot_seats} hot seats, "
        f"{sum(1 for p in plans if p['retry'])} concurrent retries", results, elapsed)

    created = collections.Counter()
    seat_owner = {}
    by_key = collections.defaultdict(list)
    for (plan, _), r in zip(jobs, results):
        by_key[plan["key"]].append(r)
        if r.status == 201:
            rid = r.body["reservation_id"]
            created[rid] += 1
            ctx.check(r.body["user_id"] == plan["user"], "reservation was issued to the wrong user")
            for seat in r.body["seats"]:
                if seat in seat_owner and seat_owner[seat] != rid:
                    ctx.fail(f"seat {seat} was confirmed to two different reservations")
                seat_owner[seat] = rid
    ctx.check(all(c == 1 for c in created.values()), "a reservation was created more than once")
    for rs in by_key.values():
        ids = {r.body["reservation_id"] for r in rs if r.status in (200, 201) and r.body}
        ctx.check(len(ids) <= 1, "retries with one idempotency key produced different reservations")
    for r in results:
        if r.replay:
            ctx.check(r.body["reservation_id"] in created, "a replay pointed at an unknown reservation")

    state = await reconcile(ctx, "stampede show", show)
    if state and not tally.get("client_error"):
        confirmed_now = {s for s, st in state["seats"].items() if st == "confirmed"}
        ctx.check(confirmed_now == set(seat_owner),
                  f"confirmed seats ({len(confirmed_now)}) differ from seats handed out in 201s ({len(seat_owner)})")
        single_targets = {p["seats"][0] for p in plans if len(p["seats"]) == 1}
        missing = single_targets - confirmed_now
        ctx.check(not missing, f"{len(missing)} seats were wanted by single-seat requests but nobody got them")
        print(f"   seats sold: {len(seat_owner)} of {n}; distinct reservations: {len(created)}")


async def phase_user_limit(ctx):
    users = [f"limit-{ctx.run}-{i}" for i in range(5)]
    show = await create_show(ctx, f"limit-{ctx.run}", [f"L{i}" for i in range(1, 51)])
    tokens = await mint_tokens(ctx.api, users)
    jobs = [(u, reserve(ctx, show, tokens[u], [f"L{ui * 10 + j + 1}"]))
            for ui, u in enumerate(users) for j in range(10)]
    started = time.perf_counter()
    results = await asyncio.gather(*(job for _, job in jobs))
    ctx.record("3. per-user limit: 5 users x 10 parallel reserves (limit 4)", results, time.perf_counter() - started)

    wins = collections.Counter()
    for (user, _), r in zip(jobs, results):
        if r.status == 201:
            wins[user] += 1
        else:
            ctx.check(r.status == 409 and r.code == "per_user_limit",
                      f"over-limit request should be a clean 409 per_user_limit, got {r.status} {r.code}")
    for user in users:
        ctx.check(wins[user] == LIMIT, f"{user} confirmed {wins[user]} seats, expected exactly {LIMIT}")
    state = await reconcile(ctx, "limit show", show)
    if state:
        ctx.check(state["confirmed"] == LIMIT * len(users), "confirmed seat count does not match the limits")


async def phase_idempotency(ctx):
    show = await create_show(ctx, f"idem-{ctx.run}", ["B1", "B2", "B3"])
    user = f"idem-{ctx.run}"
    token = (await mint_tokens(ctx.api, [user]))[user]
    key = uuid.uuid4().hex
    first = await reserve(ctx, show, token, ["B1"], key)
    again = await reserve(ctx, show, token, ["B1"], key)
    other = await reserve(ctx, show, token, ["B2"], key)
    key2 = uuid.uuid4().hex
    parallel = await asyncio.gather(*(reserve(ctx, show, token, ["B3"], key2) for _ in range(20)))
    ctx.record_calls([first, again, other, *parallel])

    print("\n== 4. idempotency")
    ctx.check(first.status == 201, f"first call should be 201, got {first.status}")
    ctx.check(again.status == 200 and again.replay and again.body == first.body,
              "retry must return the original reservation unchanged")
    ctx.check(other.status == 409 and other.code == "idempotency_key_conflict",
              "same key with different seats must be 409 idempotency_key_conflict")
    created = [r for r in parallel if r.status == 201]
    ctx.check(len(created) == 1, f"20 parallel same-key requests created {len(created)} reservations")
    ids = {r.body["reservation_id"] for r in parallel if r.status in (200, 201)}
    ctx.check(len(ids) == 1, "parallel same-key requests returned different reservations")
    state = await reconcile(ctx, "idempotency show", show)
    if state:
        ctx.check(state["seats"]["B1"] == "confirmed" and state["seats"]["B3"] == "confirmed"
                  and state["seats"]["B2"] == "available",
                  "idempotency replays or conflicts moved seats they should not have")


async def phase_identity(ctx):
    show = await create_show(ctx, f"identity-{ctx.run}", ["C1", "C2"])
    mallory, alice = f"mallory-{ctx.run}", f"alice-{ctx.run}"
    tokens = await mint_tokens(ctx.api, [mallory, alice])

    spoofed = await reserve(ctx, show, tokens[mallory], ["C1"], extra={"user_id": alice})
    ctx.check(spoofed.status == 201 and spoofed.body["user_id"] == mallory,
              "a spoofed user_id in the body must be ignored")
    rid = spoofed.body["reservation_id"] if spoofed.body else ""
    foreign = await ctx.api.call("POST", f"/reservations/{rid}/cancel", tokens[alice])
    ctx.check(foreign.status == 403, f"cancelling someone else's reservation must be refused, got {foreign.status}")
    none = await ctx.api.call("POST", f"/reservations/{rid}/cancel")
    ctx.check(none.status == 401, f"cancel without a token must be 401, got {none.status}")
    own = await ctx.api.call("POST", f"/reservations/{rid}/cancel", tokens[mallory])
    ctx.check(own.status == 200 and own.body["status"] == "cancelled", "the owner must be able to cancel")
    ctx.expected_cancelled += 1
    rebook = await reserve(ctx, show, tokens[alice], ["C1"])
    ctx.check(rebook.status == 201, f"a released seat must be bookable again, got {rebook.status}")
    again = await ctx.api.call("POST", f"/reservations/{rid}/cancel", tokens[mallory])
    ctx.check(again.status == 200, "cancelling twice must be harmless")
    ctx.record_calls([spoofed, foreign, none, own, rebook, again])

    print("\n== 5. identity and cancel")
    state = await reconcile(ctx, "identity show", show)
    if state:
        ctx.check(state["seats"]["C1"] == "confirmed",
                  "a late cancel resurrected a seat already confirmed to someone else")
    mine = await ctx.api.call("GET", f"/reservations/{rebook.body['reservation_id']}", tokens[alice]) if rebook.body else None
    if mine is not None:
        ctx.check(mine.status == 200 and mine.body["status"] == "confirmed", "the new owner's reservation changed")


# ----------------------------------------------------------------------- main


def metrics_report(ctx, before, after):
    def delta(name):
        return after.get(name, 0.0) - before.get(name, 0.0)

    seen = ctx.outcomes
    rows = [
        ("reservations_confirmed_total", delta("reservations_confirmed_total"), seen["confirmed"]),
        ("reservations_cancelled_total", delta("reservations_cancelled_total"), ctx.expected_cancelled),
        ("declined{seat_taken}", delta('reservations_declined_total{reason="seat_taken"}'), seen["declined:seat_taken"]),
        ("declined{per_user_limit}", delta('reservations_declined_total{reason="per_user_limit"}'), seen["declined:per_user_limit"]),
        ("declined{idempotent_replay}", delta('reservations_declined_total{reason="idempotent_replay"}'), seen["idempotent_replay"]),
        ("declined{idempotency_key_conflict}", delta('reservations_declined_total{reason="idempotency_key_conflict"}'), seen["declined:idempotency_key_conflict"]),
    ]
    print("\n== metrics vs observed (server delta / client count)")
    exact = not seen["client_error"]
    for name, server, client in rows:
        ok = int(server) == client
        print(f"   {name:<36}{int(server):>8}{client:>10}   {'ok' if ok else 'MISMATCH'}")
        if exact and not ok:
            ctx.fail(f"metric {name}: server counted {int(server)}, client observed {client}")
    if not exact:
        print("   (client errors occurred, so exact counter comparison is skipped)")


async def run(args):
    admin = args.admin_token or os.environ.get("ADMIN_TOKEN", "")
    host = urlparse(args.base_url).hostname or ""
    if not admin and host in ("localhost", "127.0.0.1", "::1"):
        admin = "local-admin-token"
    if not admin:
        sys.exit("ADMIN_TOKEN is required (env var or --admin-token)")

    connector = aiohttp.TCPConnector(limit=args.concurrency, ttl_dns_cache=300)
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=args.timeout)
    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        api = Api(session, args.base_url)
        ctx = Context(api, args, admin)

        ready = await api.call("GET", "/readyz")
        if ready.status != 200:
            sys.exit(f"{args.base_url}/readyz returned {ready.status or ready.error}; is the service up?")
        before = parse_metrics((await api.call("GET", "/metrics")).text)
        print(f"target {args.base_url}  run={ctx.run}  concurrency={args.concurrency}")

        started = time.perf_counter()
        await phase_hot_seat(ctx)
        await phase_stampede(ctx)
        await phase_user_limit(ctx)
        await phase_idempotency(ctx)
        await phase_identity(ctx)
        total_elapsed = time.perf_counter() - started

        after = parse_metrics((await api.call("GET", "/metrics")).text)
        metrics_report(ctx, before, after)

    print("\n== overall outcome distribution")
    for name, count in sorted(ctx.outcomes.items(), key=lambda kv: -kv[1]):
        print(f"   {name:<34}{count:>8}")
    print(f"   total requests: {sum(ctx.outcomes.values())} in {total_elapsed:.1f}s")
    print("\n== final reconciliation")
    for line in ctx.recon_lines:
        print(f"   {line}")

    if ctx.outcomes["5xx"]:
        ctx.fail(f"{ctx.outcomes['5xx']} responses were 5xx")
    if ctx.outcomes["client_error"]:
        ctx.fail(f"{ctx.outcomes['client_error']} requests got no response (try a lower --concurrency or higher --timeout)")

    print()
    if ctx.violations:
        print(f"RESULT: FAIL ({len(ctx.violations)} problems)")
        return 1
    print("RESULT: PASS")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--admin-token", default="")
    parser.add_argument("--users", type=int, default=20000, help="users in the stampede phase")
    parser.add_argument("--seats", type=int, default=2000, help="seats in the stampede show")
    parser.add_argument("--hot-seats", type=int, default=10)
    parser.add_argument("--hot-fraction", type=float, default=0.6, help="share of stampede users aiming at hot seats")
    parser.add_argument("--storm-users", type=int, default=500, help="users in the single-seat storm")
    parser.add_argument("--concurrency", type=int, default=500, help="max simultaneous connections from this client")
    parser.add_argument("--timeout", type=float, default=120.0, help="seconds to wait for a response")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    sys.exit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
