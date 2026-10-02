# Seat reservation service

A JSON HTTP API that sells assigned seats for a show and keeps them correct under heavy
concurrency: no seat is ever sold twice, a user can never exceed the per-show limit, and a
retried request never reserves twice.

Stack: Python 3.12, FastAPI, asyncpg, PostgreSQL 16, Prometheus metrics, Docker.

## Run it locally

```bash
docker compose up --build -d        # API on http://localhost:8000, Postgres on 127.0.0.1:55432
curl localhost:8000/readyz
```

Local defaults (see `docker-compose.yml`): admin token `local-admin-token`.
Interactive API docs are at `/docs`.

Without Docker: export `DATABASE_URL`, `AUTH_SECRET`, `ADMIN_TOKEN` (see `.env.example`), then

```bash
pip install -r requirements.txt
uvicorn app.main:create_app --factory --port 8000 --no-access-log
```

## Burst script (one command)

Reproduces the on-sale stampede against any running instance and prints the outcome
distribution, latency percentiles, the final reconciliation and a metrics cross-check.
It exits non-zero if any invariant is violated or any 5xx is seen.

```bash
ADMIN_TOKEN=<admin token> ./burst.sh https://your-service.example.com
# or: make burst BASE_URL=http://localhost:8000
```

Defaults: 500 users on one seat, then a 20,000-user stampede over 2,000 seats (60% aiming at
10 hot seats, 10% asking for two adjacent seats, 15% retrying with the same idempotency key),
then the per-user-limit, idempotency, spoofed-identity and cancel checks.
Tune with `--users`, `--seats`, `--hot-seats`, `--storm-users`, `--concurrency`, `--timeout`
(`./burst.sh <url> --help`). On a small free-tier instance start with `--users 5000`.

## Authentication

Every user endpoint needs `Authorization: Bearer <token>`. The user id is read from the
token and never from the request body.

* Users: `POST /auth/token` with `{"user_id": "alice"}` returns a signed token
  (HMAC-SHA256, 7 day expiry). Issuance is open so the service can be exercised end to end;
  set `OPEN_TOKEN_ISSUANCE=false` to disable it when real identity sits in front.
* Admin: the value of `ADMIN_TOKEN` is the bearer token for `POST /shows`.

## API

| Method | Path | Auth | Notes |
|---|---|---|---|
| POST | `/shows` | admin | `{"name","seats":[...],"price_paise":25000,"per_user_limit":4}` → 201, all seats `available`. `price_paise` must be an integer. |
| GET | `/shows/{id}` | none | Per-seat status, counts, `invariant_ok`. `?include_seats=false` for counts only. |
| POST | `/shows/{id}/reserve` | user | `{"seats":["A12"],"idempotency_key":"..."}` (key may also be the `Idempotency-Key` header). |
| POST | `/reservations/{id}/cancel` | owner | Releases the seats. Repeating it is harmless. |
| GET | `/reservations/{id}` | owner | |
| POST | `/auth/token` | none | See above. |
| GET | `/healthz` | none | Liveness. No dependencies. |
| GET | `/readyz` | none | Readiness. Runs `SELECT 1`; 503 when the database is unreachable. |
| GET | `/metrics` | none | Prometheus text format. |

### Reserve outcomes

| Status | `error` | Meaning |
|---|---|---|
| 201 | | Confirmed. Body: `reservation_id, show_id, user_id, seats, amount_paise, status`. |
| 200 | | Idempotent replay: the original reservation, header `Idempotent-Replay: true`. |
| 409 | `seat_taken` | At least one requested seat is not available. Nothing was reserved. |
| 409 | `per_user_limit` | The request would take the user above the show's limit. |
| 409 | `idempotency_key_conflict` | The key was already used with different seats. |
| 404 | `unknown_seat` / `show_not_found` | |
| 401 / 403 / 422 | | Missing or bad token / not the owner or not admin / malformed body. |

Behaviour that is worth knowing:

* **Multi-seat requests are all-or-nothing.** If any requested seat is taken, none is reserved.
* **Confirmed immediately, released by explicit cancel.** There is no time-boxed hold, so the
  `held` count is always 0 (it exists in the status vocabulary and is counted in the invariant).
* **Declines are not stored against the idempotency key.** Only successful reservations are, so a
  client may retry a declined request later with the same key once the seat frees up.
* A replay returns the original response verbatim. Seat order in the body does not matter when
  comparing a retry to the original.

## Deploy

### Render (free tier)

1. Push this repo to GitHub.
2. In Render choose **New → Blueprint** and select the repo. `render.yaml` creates the web
   service (Docker) and a Postgres database and wires `DATABASE_URL`; `AUTH_SECRET` and
   `ADMIN_TOKEN` are generated for you (read `ADMIN_TOKEN` from the service's Environment tab).
3. When it is live: `curl https://<service>.onrender.com/readyz`, then run the burst script.

Free instances sleep when idle, so the first request after a pause is a cold start. The service
waits up to 30 s for the database at boot and keeps retrying in the background after that;
`/readyz` stays 503 until the database answers. Free Render Postgres databases expire after a
limited period, check the current terms before relying on one.

### Any Docker host (Fly.io, Railway, a VM)

Build the `Dockerfile`, provide `DATABASE_URL`, `AUTH_SECRET`, `ADMIN_TOKEN`, and expose the
port in `$PORT` (default 8000). The schema is created automatically on first start.
Behind pgbouncer in transaction mode set `DB_STATEMENT_CACHE_SIZE=0`.

Run a single process per container; metrics live in process memory.

## Observability

**Metrics** (`/metrics`)

| Metric | Type | Meaning |
|---|---|---|
| `reservations_confirmed_total` | counter | Reservations confirmed |
| `seats_confirmed_total` | counter | Seats confirmed |
| `reservations_declined_total{reason}` | counter | `seat_taken`, `per_user_limit`, `idempotent_replay`, `idempotency_key_conflict`, `unknown_seat` |
| `reservations_cancelled_total` | counter | Reservations cancelled |
| `seats_available{show_id}` | gauge | Read from the database at scrape time, so it cannot drift from `GET /shows/{id}` |
| `seats_held{show_id}`, `seats_confirmed_current{show_id}` | gauge | Same, for the other states |
| `database_up` | gauge | Last probe result |
| `http_requests_total{method,route,status}` | counter | Watch the `5xx` series |
| `http_request_duration_seconds` | histogram | Latency per route |
| `http_requests_in_flight` | gauge | |

Counters count since process start; the gauges describe the 20 most recently created shows
(`METRICS_MAX_SHOWS`). On a fresh show, `reservations_confirmed_total` minus
`reservations_cancelled_total` equals the show's confirmed seats (single seats per reservation).

**Logs**: one JSON line per request on stdout (`ts, level, logger, msg, request_id, method, path,
route, status, duration_ms`). A client-supplied `X-Request-ID` is honoured, otherwise one is
generated, and it is echoed back in the response header. On Render the logs are in the
service's **Logs** tab; locally use `make logs`.

## Tests

```bash
pip install -r requirements-dev.txt
make test        # starts the compose database, runs unit and integration tests
```

The integration tests need a database: set `TEST_DATABASE_URL`, otherwise they are skipped.

## Configuration

| Variable | Default | |
|---|---|---|
| `DATABASE_URL` | required | |
| `AUTH_SECRET` | required | Signs user tokens |
| `ADMIN_TOKEN` | required | Bearer token for admin endpoints |
| `DB_POOL_MIN` / `DB_POOL_MAX` | 2 / 20 | Keep `DB_POOL_MAX` under your database's connection limit |
| `DB_COMMAND_TIMEOUT` | 60 | Seconds |
| `DB_STATEMENT_CACHE_SIZE` | 100 | 0 behind transaction-mode pgbouncer |
| `STARTUP_DB_WAIT` | 30 | Seconds to wait for the database at boot |
| `OPEN_TOKEN_ISSUANCE` | true | |
| `TOKEN_TTL_SECONDS` | 604800 | |
| `DEFAULT_PER_USER_LIMIT` | 4 | |
| `MAX_SEATS_PER_REQUEST` / `MAX_SEATS_PER_SHOW` | 50 / 50000 | |
| `METRICS_MAX_SHOWS` | 20 | |
| `LOG_LEVEL` | INFO | |

## Layout

```
app/main.py         routes, auth dependencies, error rendering, app factory
app/service.py      the reservation, cancel and show logic (all SQL)
app/schema.sql      tables, constraints, indexes (applied at startup)
app/db.py           pool, startup retry, dedicated readiness/metrics connections
app/metrics.py      Prometheus metrics
app/middleware.py   request id, request metrics, structured request log
scripts/burst.py    stampede and verification client
tests/              unit and integration tests
```
