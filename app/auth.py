from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
from typing import Optional

USER_ID_PATTERN = r"^[A-Za-z0-9_.@:\-]{1,64}$"
_USER_ID_RE = re.compile(USER_ID_PATTERN)


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(secret: str, body: str) -> str:
    return _b64e(hmac.new(secret.encode(), body.encode("ascii"), hashlib.sha256).digest())


def issue_token(secret: str, user_id: str, ttl_seconds: int) -> str:
    payload = json.dumps({"sub": user_id, "exp": int(time.time()) + ttl_seconds}, separators=(",", ":"))
    body = _b64e(payload.encode())
    return f"{body}.{_sign(secret, body)}"


def verify_token(secret: str, token: str) -> Optional[str]:
    """Return the user id carried by a valid, unexpired token, otherwise None."""
    try:
        body, signature = token.split(".", 1)
        if not hmac.compare_digest(signature, _sign(secret, body)):
            return None
        payload = json.loads(_b64d(body))
        if payload["exp"] < time.time():
            return None
        subject = payload["sub"]
    except (ValueError, KeyError, TypeError):
        return None
    if isinstance(subject, str) and _USER_ID_RE.match(subject):
        return subject
    return None


def bearer_token(header: Optional[str]) -> Optional[str]:
    if not header:
        return None
    scheme, _, value = header.partition(" ")
    value = value.strip()
    if scheme.lower() != "bearer" or not value:
        return None
    return value
