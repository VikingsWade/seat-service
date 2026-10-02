from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Optional

import asyncpg

from .config import Settings
from .errors import DatabaseUnavailable

log = logging.getLogger("app.db")

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
_SCHEMA_LOCK_KEY = 727301


class _SideConnection:
    """A single connection kept outside the pool for readiness and metrics queries.

    These must keep working while every pooled connection is busy serving a burst.
    """

    def __init__(self, dsn: str, statement_cache_size: int) -> None:
        self._dsn = dsn
        self._cache = statement_cache_size
        self._conn: Optional[asyncpg.Connection] = None
        self._lock = asyncio.Lock()

    async def fetch(self, sql: str, *args, timeout: float = 5.0):
        async with self._lock:
            try:
                if self._conn is None or self._conn.is_closed():
                    self._conn = await asyncpg.connect(
                        dsn=self._dsn, timeout=2.0, statement_cache_size=self._cache
                    )
                return await self._conn.fetch(sql, *args, timeout=timeout)
            except BaseException:
                await self._drop()
                raise

    async def _drop(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                await conn.close(timeout=1.0)
            except Exception:
                conn.terminate()

    async def close(self) -> None:
        async with self._lock:
            await self._drop()


class Database:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self.pool: Optional[asyncpg.Pool] = None
        self._reconnect_task: Optional[asyncio.Task] = None
        dsn, cache = settings.database_url, settings.db_statement_cache_size
        self._ready_conn = _SideConnection(dsn, cache)
        self._metrics_conn = _SideConnection(dsn, cache)

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._settings.startup_db_wait
        while True:
            try:
                await self._connect()
                return
            except Exception as exc:
                log.warning("database not ready", extra={"error": repr(exc)})
                if loop.time() >= deadline:
                    break
                await asyncio.sleep(1.0)
        self._reconnect_task = asyncio.create_task(self._reconnect_forever())

    async def _connect(self) -> None:
        s = self._settings
        pool = await asyncpg.create_pool(
            dsn=s.database_url,
            min_size=s.db_pool_min,
            max_size=s.db_pool_max,
            command_timeout=s.db_command_timeout,
            statement_cache_size=s.db_statement_cache_size,
            max_inactive_connection_lifetime=300.0,
            timeout=10.0,
        )
        try:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute("SELECT pg_advisory_xact_lock($1)", _SCHEMA_LOCK_KEY)
                    await conn.execute(SCHEMA_PATH.read_text())
        except BaseException:
            await pool.close()
            raise
        self.pool = pool
        log.info("database ready", extra={"pool_max": s.db_pool_max})

    async def _reconnect_forever(self) -> None:
        delay = 1.0
        while True:
            try:
                await self._connect()
                return
            except Exception as exc:
                log.warning("database reconnect failed", extra={"error": repr(exc)})
                await asyncio.sleep(delay)
                delay = min(delay * 2, 10.0)

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[asyncpg.Connection]:
        pool = self.pool
        if pool is None:
            raise DatabaseUnavailable("database pool is not initialised")
        async with pool.acquire() as conn:
            yield conn

    async def ping(self) -> bool:
        if self.pool is None:
            return False
        try:
            await self._ready_conn.fetch("SELECT 1", timeout=2.0)
            return True
        except Exception as exc:
            log.warning("readiness check failed", extra={"error": repr(exc)})
            return False

    async def monitor_fetch(self, sql: str, *args):
        return await self._metrics_conn.fetch(sql, *args, timeout=10.0)

    async def close(self) -> None:
        if self._reconnect_task is not None:
            self._reconnect_task.cancel()
        if self.pool is not None:
            await self.pool.close()
        await self._ready_conn.close()
        await self._metrics_conn.close()
