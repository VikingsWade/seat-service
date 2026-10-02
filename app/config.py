from __future__ import annotations

import os
from dataclasses import dataclass


def _int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    return int(raw) if raw else default


def _float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    return float(raw) if raw else default


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    database_url: str
    auth_secret: str
    admin_token: str
    db_pool_min: int = 2
    db_pool_max: int = 20
    db_command_timeout: float = 60.0
    db_statement_cache_size: int = 100
    startup_db_wait: float = 30.0
    open_token_issuance: bool = True
    token_ttl_seconds: int = 7 * 24 * 3600
    default_per_user_limit: int = 4
    max_seats_per_request: int = 50
    max_seats_per_show: int = 50000
    metrics_max_shows: int = 20
    log_level: str = "INFO"


def load_settings() -> Settings:
    missing = [n for n in ("DATABASE_URL", "AUTH_SECRET", "ADMIN_TOKEN") if not os.getenv(n)]
    if missing:
        raise RuntimeError(f"missing required environment variables: {', '.join(missing)}")
    return Settings(
        database_url=os.environ["DATABASE_URL"],
        auth_secret=os.environ["AUTH_SECRET"],
        admin_token=os.environ["ADMIN_TOKEN"],
        db_pool_min=_int("DB_POOL_MIN", 2),
        db_pool_max=_int("DB_POOL_MAX", 20),
        db_command_timeout=_float("DB_COMMAND_TIMEOUT", 60.0),
        db_statement_cache_size=_int("DB_STATEMENT_CACHE_SIZE", 100),
        startup_db_wait=_float("STARTUP_DB_WAIT", 30.0),
        open_token_issuance=_bool("OPEN_TOKEN_ISSUANCE", True),
        token_ttl_seconds=_int("TOKEN_TTL_SECONDS", 7 * 24 * 3600),
        default_per_user_limit=_int("DEFAULT_PER_USER_LIMIT", 4),
        max_seats_per_request=_int("MAX_SEATS_PER_REQUEST", 50),
        max_seats_per_show=_int("MAX_SEATS_PER_SHOW", 50000),
        metrics_max_shows=_int("METRICS_MAX_SHOWS", 20),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
    )
