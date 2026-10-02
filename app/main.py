import hmac
import logging
import uuid
from contextlib import asynccontextmanager
from typing import Optional

import asyncpg
from fastapi import APIRouter, Depends, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from .auth import bearer_token, issue_token, verify_token
from .config import Settings, load_settings
from .db import Database
from .errors import ApiError, DatabaseUnavailable
from .logging_config import configure_logging
from .metrics import Metrics
from .middleware import RequestContextMiddleware
from .schemas import CreateShowRequest, ReserveRequest, TokenRequest
from .service import SeatService

log = logging.getLogger("app")
router = APIRouter()


# ------------------------------------------------------------------ auth deps


async def current_user(request: Request) -> str:
    token = bearer_token(request.headers.get("authorization"))
    if token is None:
        raise ApiError(401, "unauthenticated", "a bearer token is required")
    user_id = verify_token(request.app.state.settings.auth_secret, token)
    if user_id is None:
        raise ApiError(401, "invalid_token", "the bearer token is invalid or expired")
    return user_id


async def require_admin(request: Request) -> None:
    settings = request.app.state.settings
    token = bearer_token(request.headers.get("authorization"))
    if token is None:
        raise ApiError(401, "unauthenticated", "a bearer token is required")
    if hmac.compare_digest(token.encode(), settings.admin_token.encode()):
        return
    if verify_token(settings.auth_secret, token) is not None:
        raise ApiError(403, "forbidden", "admin access required")
    raise ApiError(401, "invalid_token", "the bearer token is invalid or expired")


# --------------------------------------------------------------------- routes


@router.get("/")
async def root():
    return {"service": "seat-reservation", "docs": "/docs", "health": "/healthz", "ready": "/readyz"}


@router.get("/healthz")
async def healthz():
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(request: Request):
    if await request.app.state.db.ping():
        return {"status": "ready"}
    return JSONResponse(status_code=503, content={"status": "unavailable", "dependency": "database"})


@router.get("/metrics")
async def metrics_endpoint(request: Request):
    state = request.app.state
    payload = await state.metrics.render(state.db)
    return Response(payload, media_type=state.metrics.content_type)


@router.post("/auth/token")
async def create_token(body: TokenRequest, request: Request):
    settings = request.app.state.settings
    if not settings.open_token_issuance:
        raise ApiError(404, "not_found", "token issuance is disabled")
    token = issue_token(settings.auth_secret, body.user_id, settings.token_ttl_seconds)
    return {
        "token": token,
        "token_type": "Bearer",
        "user_id": body.user_id,
        "expires_in": settings.token_ttl_seconds,
    }


@router.post("/shows", status_code=201, dependencies=[Depends(require_admin)])
async def create_show(body: CreateShowRequest, request: Request):
    state = request.app.state
    if len(body.seats) > state.settings.max_seats_per_show:
        raise ApiError(
            422, "too_many_seats", f"a show may have at most {state.settings.max_seats_per_show} seats"
        )
    limit = body.per_user_limit or state.settings.default_per_user_limit
    return await state.service.create_show(body.name, body.seats, body.price_paise, limit)


@router.get("/shows/{show_id}")
async def get_show(show_id: uuid.UUID, request: Request, include_seats: bool = Query(True)):
    return await request.app.state.service.get_show(show_id, include_seats)


@router.post("/shows/{show_id}/reserve")
async def reserve(
    show_id: uuid.UUID,
    body: ReserveRequest,
    request: Request,
    user_id: str = Depends(current_user),
    idempotency_header: Optional[str] = Header(default=None, alias="Idempotency-Key"),
):
    state = request.app.state
    if len(body.seats) > state.settings.max_seats_per_request:
        raise ApiError(
            422, "too_many_seats", f"at most {state.settings.max_seats_per_request} seats per request"
        )
    key = body.idempotency_key
    if idempotency_header:
        if key is not None and key != idempotency_header:
            raise ApiError(
                422, "idempotency_key_mismatch", "header and body idempotency keys differ"
            )
        key = idempotency_header
    if key is not None and not 1 <= len(key) <= 200:
        raise ApiError(422, "invalid_idempotency_key", "idempotency key must be 1-200 characters")

    outcome = await state.service.reserve(user_id, show_id, body.seats, key)
    headers = {"Idempotent-Replay": "true"} if outcome.replay else None
    return JSONResponse(status_code=outcome.status, content=outcome.body, headers=headers)


@router.post("/reservations/{reservation_id}/cancel")
async def cancel_reservation(
    reservation_id: uuid.UUID, request: Request, user_id: str = Depends(current_user)
):
    return await request.app.state.service.cancel(user_id, reservation_id)


@router.get("/reservations/{reservation_id}")
async def get_reservation(
    reservation_id: uuid.UUID, request: Request, user_id: str = Depends(current_user)
):
    return await request.app.state.service.get_reservation(user_id, reservation_id)


# ------------------------------------------------------------ error rendering

_DB_ERRORS = (
    DatabaseUnavailable,
    asyncpg.exceptions.PostgresConnectionError,
    asyncpg.exceptions.InterfaceError,
    asyncpg.exceptions.TooManyConnectionsError,
    asyncpg.exceptions.CannotConnectNowError,
    ConnectionError,
    TimeoutError,
)


def _register_error_handlers(app: FastAPI) -> None:
    async def api_error(request: Request, exc: ApiError):
        return JSONResponse(
            status_code=exc.status,
            content={"error": exc.code, "message": exc.message, **exc.extra},
        )

    async def http_error(request: Request, exc: StarletteHTTPException):
        code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": code, "message": str(exc.detail)},
            headers=getattr(exc, "headers", None),
        )

    async def validation_error(request: Request, exc: RequestValidationError):
        details = [
            {"loc": [str(part) for part in err["loc"]], "msg": err["msg"], "type": err["type"]}
            for err in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content={"error": "invalid_request", "message": "request validation failed", "details": details},
        )

    async def database_error(request: Request, exc: Exception):
        log.warning("database unavailable", extra={"error": repr(exc)})
        return JSONResponse(
            status_code=503,
            content={"error": "database_unavailable", "message": "the datastore is unavailable"},
            headers={"Retry-After": "2"},
        )

    app.add_exception_handler(ApiError, api_error)
    app.add_exception_handler(StarletteHTTPException, http_error)
    app.add_exception_handler(RequestValidationError, validation_error)
    for exc_type in _DB_ERRORS:
        app.add_exception_handler(exc_type, database_error)


# ------------------------------------------------------------------- factory


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    settings = settings or load_settings()
    configure_logging(settings.log_level)

    metrics = Metrics(settings.metrics_max_shows)
    db = Database(settings)
    service = SeatService(db, metrics, settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await db.start()
        try:
            yield
        finally:
            await db.close()

    app = FastAPI(title="Seat reservation service", version="1.0.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.db = db
    app.state.metrics = metrics
    app.state.service = service

    app.add_middleware(RequestContextMiddleware, metrics=metrics)
    app.include_router(router)
    _register_error_handlers(app)
    return app
