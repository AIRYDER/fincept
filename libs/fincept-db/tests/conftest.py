from __future__ import annotations

import os
import socket
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from fincept_core.config import Settings
from fincept_db import engine as db_engine
from fincept_db.models import Base

TEST_DB_NAME = "fincept_test"
ADMIN_URL = "postgresql+asyncpg://fincept:fincept@localhost:5432/postgres"
TEST_DB_URL = f"postgresql+asyncpg://fincept:fincept@localhost:5432/{TEST_DB_NAME}"


def _postgres_reachable() -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.5)
    try:
        sock.connect(("localhost", 5432))
        return True
    except OSError:
        return False
    finally:
        sock.close()


@pytest_asyncio.fixture(scope="session", autouse=True)
async def _schema() -> AsyncIterator[None]:
    if not _postgres_reachable():
        pytest.skip("requires postgres+timescale at :5432")

    original_db_url = os.environ.get("FINCEPT_DB_URL")
    original_test_nullpool = os.environ.get("FINCEPT_DB_TEST_NULLPOOL")

    admin = create_async_engine(ADMIN_URL, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    try:
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{TEST_DB_NAME}" WITH (FORCE)'))
            await conn.execute(text(f'CREATE DATABASE "{TEST_DB_NAME}"'))
    except Exception as exc:
        await admin.dispose()
        pytest.skip(f"cannot prepare test database: {type(exc).__name__}: {exc}")
    await admin.dispose()

    os.environ["FINCEPT_DB_URL"] = TEST_DB_URL
    os.environ["FINCEPT_DB_TEST_NULLPOOL"] = "1"
    Settings.clear_cache()
    await db_engine.reset_engine()

    eng = db_engine.get_engine()
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await db_engine.reset_engine()

    yield

    await db_engine.reset_engine()

    admin = create_async_engine(ADMIN_URL, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    try:
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{TEST_DB_NAME}" WITH (FORCE)'))
    finally:
        await admin.dispose()
        await db_engine.reset_engine()
        if original_db_url is None:
            os.environ.pop("FINCEPT_DB_URL", None)
        else:
            os.environ["FINCEPT_DB_URL"] = original_db_url
        if original_test_nullpool is None:
            os.environ.pop("FINCEPT_DB_TEST_NULLPOOL", None)
        else:
            os.environ["FINCEPT_DB_TEST_NULLPOOL"] = original_test_nullpool
        Settings.clear_cache()


@pytest_asyncio.fixture(autouse=True)
async def _truncate_tables() -> AsyncIterator[None]:
    await db_engine.reset_engine()
    eng = db_engine.get_engine()
    async with eng.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            await conn.execute(text(f'TRUNCATE TABLE "{table.name}" CASCADE'))
    yield
    await db_engine.reset_engine()
