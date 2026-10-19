"""Schema upgrades at start-up (Alembic), with a safety net.

An install is never recreated: missing tables/columns are added, existing data is
left alone. Whenever the schema revision is about to change, a SQLite database is
first copied into <data>/backups, so a bad release is undone by restoring that
file and pinning the previous image tag. Migrations are additive-first; see
docs/PLAN-v0.3.md for the contract.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine

from .config import BASE_DIR, SPOT_DATA_DIR

logger = logging.getLogger("spotwebng")

SCRIPT_LOCATION = str(BASE_DIR / "migrations")
KEEP_PRE_MIGRATION_BACKUPS = 5
# Filled in by upgrade_database(); shown by /healthz.
SCHEMA_REVISION: str | None = None


def _config(connection: Connection) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", SCRIPT_LOCATION)
    cfg.attributes["connection"] = connection
    return cfg


def head_revision() -> str:
    cfg = Config()
    cfg.set_main_option("script_location", SCRIPT_LOCATION)
    return ScriptDirectory.from_config(cfg).get_current_head() or ""


def backup_sqlite(db_path: str, from_rev: str | None, to_rev: str) -> Path | None:
    """Copy a SQLite file with the online-backup API (safe while in WAL mode)."""
    source = Path(db_path)
    if not source.is_file():
        return None
    target_dir = SPOT_DATA_DIR / "backups"
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    target = target_dir / f"pre-migrate-{from_rev or 'none'}-to-{to_rev}-{stamp}.db"
    src, dst = sqlite3.connect(source), sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    old = sorted(target_dir.glob("pre-migrate-*.db"))
    for stale in old[:-KEEP_PRE_MIGRATION_BACKUPS]:
        stale.unlink(missing_ok=True)
    return target


def _upgrade(connection: Connection, backup_path: str | None) -> dict:
    current = MigrationContext.configure(connection).get_current_revision()
    head = head_revision()
    has_data = bool(connection.dialect.has_table(connection, "spots"))
    result = {"from": current, "to": head, "backup": None}
    if current != head:
        if backup_path and has_data:
            # Release the connection's snapshot first: sqlite's backup needs the file idle.
            connection.commit()
            made = backup_sqlite(backup_path, current, head)
            result["backup"] = str(made) if made else None
        command.upgrade(_config(connection), "head")
        connection.commit()
    return result


async def upgrade_database(engine: AsyncEngine) -> dict:
    """Bring the database to the current schema. Returns what was done."""
    global SCHEMA_REVISION
    db_path = engine.url.database if engine.url.get_backend_name() == "sqlite" else None
    if db_path in (None, "", ":memory:"):
        db_path = None
    async with engine.connect() as conn:
        result = await conn.run_sync(_upgrade, db_path)
    SCHEMA_REVISION = result["to"]
    if result["from"] != result["to"]:
        logger.info(
            "database schema %s -> %s (backup: %s)", result["from"], result["to"], result["backup"]
        )
    return result
