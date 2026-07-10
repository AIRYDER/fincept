from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from fincept_core.config import get_settings

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None

_sync_engine: Engine | None = None
_sync_sessionmaker: sessionmaker[Session] | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        url = get_settings().DB_URL
        if not url:
            raise RuntimeError("FINCEPT_DB_URL is empty; set it to a postgresql+asyncpg:// URL")
        if os.getenv("FINCEPT_DB_TEST_NULLPOOL") == "1":
            _engine = create_async_engine(
                url,
                poolclass=NullPool,
                pool_pre_ping=True,
                future=True,
            )
            return _engine
        _engine = create_async_engine(
            url,
            pool_size=20,
            max_overflow=10,
            pool_pre_ping=True,
            future=True,
        )
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _sessionmaker


async def reset_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    sm = get_sessionmaker()
    async with sm() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


# ---------------------------------------------------------------------------
# Sync engine — for the trusted-side callback writer path.
#
# The CallbackProcessor is sync; the async engine above is for the async API
# paths. The DB-backed callback sinks (dossier, shadow ledger, receipt, DLQ,
# metrics) use a sync SQLAlchemy engine + sync sessions so the processor does
# not need to become async. This adds a second connection pool — acceptable
# for the first cut (see references/fincept-db-schema.md, option 1).
#
# The async DB_URL (postgresql+asyncpg://...) is converted to a sync psycopg
# URL (postgresql+psycopg://...). Tests inject a SQLite engine directly via
# the sink constructors, so this function is only called in production.
# ---------------------------------------------------------------------------


def _async_url_to_sync(url: str) -> str:
    """Convert an asyncpg DB URL to a sync psycopg URL.

    ``postgresql+asyncpg://user:pw@host:port/db`` ->
    ``postgresql+psycopg://user:pw@host:port/db``

    Non-postgres URLs (e.g. sqlite) are returned unchanged.
    """
    if url.startswith("postgresql+asyncpg://"):
        return "postgresql+psycopg://" + url[len("postgresql+asyncpg://") :]
    return url


def get_sync_engine() -> Engine:
    """Return the lazily-created sync SQLAlchemy engine.

    Uses ``FINCEPT_DB_URL`` (same as the async engine) but converts the
    asyncpg driver to psycopg. Set ``FINCEPT_DB_TEST_NULLPOOL=1`` to use a
    NullPool (for tests / short-lived processes).
    """
    global _sync_engine
    if _sync_engine is None:
        url = get_settings().DB_URL
        if not url:
            raise RuntimeError("FINCEPT_DB_URL is empty; set it to a postgresql+asyncpg:// URL")
        sync_url = _async_url_to_sync(url)
        kwargs: dict[str, object] = {"future": True}
        if os.getenv("FINCEPT_DB_TEST_NULLPOOL") == "1":
            kwargs["poolclass"] = NullPool
        else:
            kwargs["pool_size"] = 20
            kwargs["max_overflow"] = 10
            kwargs["pool_pre_ping"] = True
        _sync_engine = create_engine(sync_url, **kwargs)
    return _sync_engine


def get_sync_sessionmaker() -> sessionmaker[Session]:
    """Return the lazily-created sync sessionmaker."""
    global _sync_sessionmaker
    if _sync_sessionmaker is None:
        _sync_sessionmaker = sessionmaker(get_sync_engine(), expire_on_commit=False)
    return _sync_sessionmaker


def reset_sync_engine() -> None:
    """Dispose and reset the sync engine + sessionmaker (for tests)."""
    global _sync_engine, _sync_sessionmaker
    if _sync_engine is not None:
        _sync_engine.dispose()
    _sync_engine = None
    _sync_sessionmaker = None


@contextmanager
def sync_session_scope() -> Iterator[Session]:
    """Sync context manager: commit on success, rollback on exception."""
    sm = get_sync_sessionmaker()
    with sm() as session:
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
