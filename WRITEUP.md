# Write-up

## The atomic decision

Each reserve call is one Postgres transaction (READ COMMITTED). The decision is made by
**row locks on the seat rows, taken in label order, and re-read under the lock**:

1. `pg_advisory_xact_lock` on `(user, show)`. This only serialises one user's own requests.
2. Cheap unlocked pre-check: if a requested seat is already `held`/`confirmed`, decline now. This
   exists purely so that losers of a hot-seat storm do not queue behind the winner.
3. `SELECT ... WHERE label = ANY($seats) ORDER BY label FOR UPDATE`. Exactly one transaction at a
   time holds a given seat row. Everyone else blocks here, and when the winner commits, READ
   COMMITTED re-evaluates the row and shows them `confirmed`, so they decline.
4. Only if every requested seat is `available` under the lock do we insert the reservation and
   `UPDATE seats SET status='confirmed' ... WHERE status='available'`. The update's row count is
   checked against the number of seats as a second guard.

Why it is race-free: the check and the write happen while holding the row lock, so no other
transaction can change the seat between them. The primary key `(show_id, label)` plus a `CHECK`
constraint (`available` ⇔ no owner) make an inconsistent row impossible to store.

**Multi-seat and deadlock.** Locks are always acquired in `ORDER BY label` order (the column uses
the `C` collation so the order is deterministic), by reserve and by cancel alike. Two requests for
`[A1,A2,A3]` and `[A3,A2,A1]` therefore queue on `A1` instead of each holding one end. As a
backstop, deadlock and serialization errors are caught and the transaction is retried with jitter.
Multi-seat requests are all-or-nothing: the transaction rolls back if any seat is unavailable.

## Idempotency

The key is stored in `idempotency_keys (user_id, key)` (primary key) with a SHA-256 of the request
(show id plus the sorted seat list) and the exact response body. The row is inserted **in the same
transaction** as the seat update, so a crash can never leave a reservation without its key or a
key without its reservation.

* Same key, same body: the lookup (made under the per-user advisory lock) finds the stored row and
  returns the original response with `200` and `Idempotent-Replay: true`. Two identical requests in
  flight at once are serialised by that lock, so exactly one creates the reservation.
* Same key, different seats: the hashes differ, so the response is `409 idempotency_key_conflict`.
* Keys are scoped per user, so one user's key can never replay another user's reservation.
* Declines are not stored, which lets a client retry a declined request after a seat frees up.
* The advisory lock is per show; the same key used for two different shows at the same instant is
  caught by the primary key (`ON CONFLICT DO NOTHING`), the transaction restarts and then takes
  the replay or conflict path.

## Holds and expiry

Explicit cancel, no time-boxed hold. A reservation is `confirmed` immediately; `POST
/reservations/{id}/cancel` frees its seats. Cancel locks the reservation row, then its seat rows in
label order, and updates seats **only where `reservation_id` equals the cancelled reservation**.
If the seat has since been booked by someone else it carries a different `reservation_id`, so a
late or repeated cancel cannot resurrect or steal it. Only the owner can cancel (403 otherwise).

## Consistency vs availability under a partition

The service is a CP system. All state lives in one Postgres primary and every decision is a
transaction against it, so it prefers refusing to answering wrongly. If the database is
unreachable `/readyz` returns 503 (it uses its own connection, not the pool, so it still works when
the pool is saturated), API calls return 503 with `Retry-After`, and nothing is ever decided from a
cache or a replica. Reads (`GET /shows/{id}`) also go to the primary, in a repeatable-read
snapshot so the counts are mutually consistent. The cost is that a partition between the app and
the database makes booking unavailable until it heals; for a system of record for unique seats
that is the right trade.

## Observability: what I would be paged for at 2am

* `/readyz` failing or `database_up == 0` for more than a minute: nothing can be sold.
* Any sustained `http_requests_total{status=~"5.."}` rate: the correctness bar is zero 5xx.
* p99 of `http_request_duration_seconds` on the reserve route above a few seconds, or
  `http_requests_in_flight` climbing without falling: the pool is saturated or locks are piling up.
* `seats_available + seats_held + seats_confirmed_current` not equal to the show's total, or
  `GET /shows/{id}` reporting `invariant_ok: false`: the one alert that means data corruption.
* Not a page but worth a dashboard: the `reservations_declined_total` split by reason. A jump in
  `idempotency_key_conflict` usually means a client bug; a jump in `seat_taken` at on-sale time is
  normal.

Every request log line carries a `request_id` that is also returned in the `X-Request-ID` header,
so a customer's failed call can be found directly.

## AI usage

[Write this section yourself, in your own words: which parts you directed versus which parts you
decided, what you changed or rejected, and what you verified by running it.]

## What I would do next

* Time-boxed holds with a payment step (`held` → `confirmed`), expiring through a sweeper or a
  `held_until` column checked in the same guarded update.
* A real identity provider instead of open token issuance; rate limiting per user and per IP.
* Shard the work by show: partition `seats` by `show_id` and put very large on-sales on their own
  database or queue front, since one hot show is bounded by a single primary's write throughput.
* A transactional outbox for booking events, and purging old idempotency keys on a schedule.
* Run behind pgbouncer, add a metrics-only read path, and add tracing around the reserve
  transaction to see lock wait time separately from query time.
