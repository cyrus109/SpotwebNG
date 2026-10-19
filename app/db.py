"""Async database layer — SQLAlchemy 2.0.

Production target is Postgres (asyncpg); a local SQLite file is the zero-config
dev fallback. Same engine abstraction either way.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from .config import get_settings

_settings = get_settings()
engine = create_async_engine(_settings.database_url, echo=False, future=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def init_db() -> None:
    """Bring the schema up to date (Alembic; see migrate.py). Never recreates an
    existing database."""
    from .migrate import upgrade_database

    await upgrade_database(engine)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session


# SQLite defaults (rollback journal, 5s busy timeout) make a writer block every
# reader — with two background loops that surfaces as "database is locked".
if engine.url.get_backend_name() == "sqlite":

    @event.listens_for(engine.sync_engine, "connect")
    def _sqlite_pragmas(dbapi_connection, _record):  # pragma: no cover - driver hook
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.close()
