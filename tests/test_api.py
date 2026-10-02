import asyncio
import collections
import uuid

ADMIN = {"Authorization": "Bearer test-admin-token"}


async def token(client, user):
    r = await client.post("/auth/token", json={"user_id": user})
    assert r.status_code == 200
    return r.json()["token"]


async def new_show(client, seats, limit=None):
    body = {"name": f"show-{uuid.uuid4().hex[:6]}", "seats": seats, "price_paise": 25000}
    if limit:
        body["per_user_limit"] = limit
    r = await client.post("/shows", json=body, headers=ADMIN)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def reserve(client, show, tok, seats, key=None, extra=None):
    body = {"seats": seats, "idempotency_key": key or uuid.uuid4().hex}
    body.update(extra or {})
    return client.post(f"/shows/{show}/reserve", json=body, headers={"Authorization": f"Bearer {tok}"})


async def state(client, show):
    r = await client.get(f"/shows/{show}")
    assert r.status_code == 200
    body = r.json()
    assert body["available"] + body["held"] + body["confirmed"] == body["total_seats"]
    return body


async def test_create_show_and_auth(client):
    r = await client.post("/shows", json={"name": "x", "seats": ["A1"], "price_paise": 100})
    assert r.status_code == 401
    user = await token(client, "someone")
    r = await client.post(
        "/shows", json={"name": "x", "seats": ["A1"], "price_paise": 100},
        headers={"Authorization": f"Bearer {user}"},
    )
    assert r.status_code == 403
    r = await client.post("/shows", json={"name": "x", "seats": ["A1"], "price_paise": 1.5}, headers=ADMIN)
    assert r.status_code == 422
    show = await new_show(client, ["A1", "A2", "A3"])
    s = await state(client, show)
    assert s["available"] == 3 and set(s["seats"].values()) == {"available"}


async def test_hot_seat_has_exactly_one_winner(client):
    show = await new_show(client, [f"A{i}" for i in range(1, 21)])
    tokens = [await token(client, f"hot{i}") for i in range(150)]
    responses = await asyncio.gather(*(reserve(client, show, t, ["A12"]) for t in tokens))
    assert collections.Counter(r.status_code for r in responses) == {201: 1, 409: 149}
    s = await state(client, show)
    assert s["confirmed"] == 1 and s["seats"]["A12"] == "confirmed"


async def test_multi_seat_is_all_or_nothing_and_never_deadlocks(client):
    for _ in range(10):
        show = await new_show(client, ["A1", "A2", "A3", "A4"])
        a, b = await token(client, "a"), await token(client, "b")
        r1, r2 = await asyncio.gather(
            reserve(client, show, a, ["A1", "A2", "A3"]),
            reserve(client, show, b, ["A3", "A2", "A1"]),
        )
        assert sorted([r1.status_code, r2.status_code]) == [201, 409]
        s = await state(client, show)
        assert s["confirmed"] == 3 and s["available"] == 1


async def test_per_user_limit_under_concurrency(client):
    show = await new_show(client, [f"B{i}" for i in range(1, 21)])
    tok = await token(client, "greedy")
    responses = await asyncio.gather(*(reserve(client, show, tok, [f"B{i}"]) for i in range(1, 11)))
    codes = collections.Counter(r.status_code for r in responses)
    assert codes == {201: 4, 409: 6}
    assert {r.json()["error"] for r in responses if r.status_code == 409} == {"per_user_limit"}
    assert (await state(client, show))["confirmed"] == 4


async def test_idempotent_retry_and_key_conflict(client):
    show = await new_show(client, ["A1", "A2"])
    tok = await token(client, "retrier")
    first = await reserve(client, show, tok, ["A1"], key="k-1")
    again = await reserve(client, show, tok, ["A1"], key="k-1")
    assert first.status_code == 201 and again.status_code == 200
    assert again.json() == first.json()
    clash = await reserve(client, show, tok, ["A2"], key="k-1")
    assert clash.status_code == 409 and clash.json()["error"] == "idempotency_key_conflict"
    s = await state(client, show)
    assert s["confirmed"] == 1 and s["seats"]["A2"] == "available"

    parallel = await asyncio.gather(*(reserve(client, show, tok, ["A2"], key="k-2") for _ in range(15)))
    assert sum(r.status_code == 201 for r in parallel) == 1
    assert len({r.json()["reservation_id"] for r in parallel}) == 1


async def test_identity_comes_from_the_token(client):
    show = await new_show(client, ["A1", "A2"])
    mallory, alice = await token(client, "mallory"), await token(client, "alice")
    r = await reserve(client, show, mallory, ["A1"], extra={"user_id": "alice"})
    assert r.status_code == 201 and r.json()["user_id"] == "mallory"
    rid = r.json()["reservation_id"]
    forbidden = await client.post(
        f"/reservations/{rid}/cancel", headers={"Authorization": f"Bearer {alice}"}
    )
    assert forbidden.status_code == 403
    assert (await state(client, show))["seats"]["A1"] == "confirmed"


async def test_cancel_releases_but_never_resurrects(client):
    show = await new_show(client, ["A1"])
    mallory, alice = await token(client, "m2"), await token(client, "a2")
    first = await reserve(client, show, mallory, ["A1"])
    rid = first.json()["reservation_id"]
    headers = {"Authorization": f"Bearer {mallory}"}
    assert (await client.post(f"/reservations/{rid}/cancel", headers=headers)).status_code == 200
    assert (await state(client, show))["available"] == 1
    assert (await reserve(client, show, alice, ["A1"])).status_code == 201
    assert (await client.post(f"/reservations/{rid}/cancel", headers=headers)).status_code == 200
    assert (await state(client, show))["seats"]["A1"] == "confirmed"


async def test_unknown_seat_and_validation_are_clean_4xx(client):
    show = await new_show(client, ["A1"])
    tok = await token(client, "tidy")
    assert (await reserve(client, show, tok, ["Z9"])).status_code == 404
    assert (await reserve(client, show, tok, ["A1", "A1"])).status_code == 422
    assert (await reserve(client, str(uuid.uuid4()), tok, ["A1"])).status_code == 404
    r = await client.post(f"/shows/{show}/reserve", json={"seats": ["A1"]})
    assert r.status_code == 401


async def test_health_ready_and_metrics(client):
    assert (await client.get("/healthz")).status_code == 200
    assert (await client.get("/readyz")).status_code == 200
    text = (await client.get("/metrics")).text
    for name in ("reservations_confirmed_total", "reservations_declined_total", "seats_available"):
        assert name in text
