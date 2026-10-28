"""Async database layer — SQLAlchemy 2.0.

Production target is Postgres (asyncpg); a local SQLite file is the zero-config
dev fallback. Same engine abstraction either way.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

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


# SQLite defaults (rollback journal, 5s busy timeout, a 2 MB page cache) are wrong for a
# multi-gigabyte index served by two background loops and a web UI: writers block readers
# ("database is locked") and a tiny cache means the same pages are read from disk over and over.
SQLITE_PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA busy_timeout=30000",
    # Cut the write-ahead log back to 128 MiB whenever a checkpoint empties it. Without a
    # limit the file keeps the size of its busiest moment forever (we have seen 8 GB).
    "PRAGMA journal_size_limit=134217728",
    "PRAGMA cache_size=-16384",  # 16 MiB per connection instead of 2 MiB
    "PRAGMA mmap_size=268435456",  # let the OS cache serve hot pages (256 MiB window)
    "PRAGMA temp_store=MEMORY",  # sorts and temp b-trees stay off the disk
)


def configure_sqlite_connection(dbapi_connection: Any) -> None:
    cur = dbapi_connection.cursor()
    for pragma in SQLITE_PRAGMAS:
        cur.execute(pragma)
    cur.close()


def install_sqlite_pragmas(target: AsyncEngine) -> None:
    @event.listens_for(target.sync_engine, "connect")
    def _on_connect(dbapi_connection, _record):  # pragma: no cover - driver hook
        configure_sqlite_connection(dbapi_connection)


if engine.url.get_backend_name() == "sqlite":
    install_sqlite_pragmas(engine)


CHECKPOINT_MODES = ("PASSIVE", "FULL", "RESTART", "TRUNCATE")
WAL_SOFT_BYTES = 256 * 1024 * 1024  # above this, try a passive checkpoint
WAL_HARD_BYTES = (
    1024 * 1024 * 1024
)  # still above this afterwards: truncate (briefly blocks writers)


async def checkpoint(
    bind: AsyncEngine | None = None, mode: str = "PASSIVE"
) -> tuple[int, int, int] | None:
    """Fold the write-ahead log back into the database file. Returns SQLite's
    (busy, log frames, checkpointed frames), or None for other databases.

    PASSIVE never blocks anyone but cannot pass a reader that is still open;
    TRUNCATE waits (up to the busy timeout) for readers and then empties the file."""
    if mode not in CHECKPOINT_MODES:
        raise ValueError(mode)
    target = bind or engine
    if target.url.get_backend_name() != "sqlite":
        return None
    async with target.connect() as conn:
        row = (await conn.exec_driver_sql(f"PRAGMA wal_checkpoint({mode})")).first()
    return (int(row[0]), int(row[1]), int(row[2])) if row else None


def wal_bytes(bind: AsyncEngine | None = None) -> int:
    target = bind or engine
    if target.url.get_backend_name() != "sqlite":
        return 0
    database = target.url.database
    if not database or database == ":memory:":
        return 0
    path = Path(f"{database}-wal")
    return path.stat().st_size if path.exists() else 0


async def tame_wal(
    bind: AsyncEngine | None = None, soft: int = WAL_SOFT_BYTES, hard: int = WAL_HARD_BYTES
) -> int:
    """Keep the log file small: passive checkpoint above `soft`, truncating one above
    `hard`. Returns the log size afterwards. Never raises (a busy database just waits)."""
    try:
        if wal_bytes(bind) > soft:
            await checkpoint(bind, "PASSIVE")
        if wal_bytes(bind) > hard:
            await checkpoint(bind, "TRUNCATE")
    except Exception:  # a checkpoint is housekeeping; it must never break the caller
        logging.getLogger("spotwebng").exception("WAL checkpoint failed")
    return wal_bytes(bind)


async def checkpoint_after_bulk_work(bind: AsyncEngine | None = None) -> None:
    """A passive checkpoint after imports, backups, jobs and retrieval runs: they write a
    lot and often end while the log is large."""
    try:
        await checkpoint(bind, "PASSIVE")
    except Exception:
        logging.getLogger("spotwebng").exception("WAL checkpoint failed")
