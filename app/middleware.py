from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid

from .logging_config import request_id_var

log = logging.getLogger("app.request")

_QUIET_PATHS = {"/healthz", "/readyz", "/metrics"}


async def _send_internal_error(send, request_id: str) -> None:
    payload = json.dumps(
        {"error": "internal_error", "message": "internal server error", "request_id": request_id}
    ).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 500,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(payload)).encode()),
                (b"x-request-id", request_id.encode("latin-1")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": payload})


class RequestContextMiddleware:
    """Assigns a request id, records metrics and writes one structured log line per request."""

    def __init__(self, app, metrics) -> None:
        self.app = app
        self.metrics = metrics

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = ""
        for name, value in scope["headers"]:
            if name == b"x-request-id":
                request_id = value.decode("latin-1")[:64]
                break
        if not request_id or not request_id.isprintable():
            request_id = uuid.uuid4().hex

        token = request_id_var.set(request_id)
        started = time.perf_counter()
        status = 500
        response_started = False

        async def send_wrapper(message) -> None:
            nonlocal status, response_started
            if message["type"] == "http.response.start":
                status = message["status"]
                response_started = True
                headers = list(message.get("headers") or [])
                headers.append((b"x-request-id", request_id.encode("latin-1")))
                message = {**message, "headers": headers}
            await send(message)

        self.metrics.in_flight.inc()
        try:
            await self.app(scope, receive, send_wrapper)
        except asyncio.CancelledError:
            status = 499
            raise
        except Exception:
            status = 500
            log.exception("unhandled error", extra={"method": scope["method"], "path": scope["path"]})
            if not response_started:
                await _send_internal_error(send, request_id)
        finally:
            elapsed = time.perf_counter() - started
            self.metrics.in_flight.dec()
            route = getattr(scope.get("route"), "path", None) or "unmatched"
            method = scope["method"]
            self.metrics.http_requests.labels(method, route, str(status)).inc()
            self.metrics.http_latency.labels(method, route).observe(elapsed)
            if status >= 500:
                level = logging.ERROR
            elif scope["path"] in _QUIET_PATHS:
                level = logging.DEBUG
            else:
                level = logging.INFO
            log.log(
                level,
                "request",
                extra={
                    "method": method,
                    "path": scope["path"],
                    "route": route,
                    "status": status,
                    "duration_ms": round(elapsed * 1000, 2),
                },
            )
            request_id_var.reset(token)
