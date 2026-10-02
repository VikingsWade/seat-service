import time
import uuid

from app.auth import bearer_token, issue_token, verify_token
from app.service import request_hash


def test_token_round_trip_and_tamper_detection():
    token = issue_token("secret", "alice", 60)
    assert verify_token("secret", token) == "alice"
    assert verify_token("other-secret", token) is None
    body, sig = token.split(".")
    assert verify_token("secret", body + "x." + sig) is None
    assert verify_token("secret", "garbage") is None


def test_expired_token_is_rejected():
    assert verify_token("secret", issue_token("secret", "alice", -1)) is None


def test_bearer_parsing():
    assert bearer_token("Bearer abc") == "abc"
    assert bearer_token("bearer abc") == "abc"
    assert bearer_token("Basic abc") is None
    assert bearer_token(None) is None
    assert bearer_token("Bearer ") is None


def test_request_hash_ignores_seat_order_but_not_content():
    show = uuid.uuid4()
    assert request_hash(show, ["A1", "A2"]) == request_hash(show, ["A2", "A1"])
    assert request_hash(show, ["A1"]) != request_hash(show, ["A2"])
    assert request_hash(show, ["A1"]) != request_hash(uuid.uuid4(), ["A1"])
