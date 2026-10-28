"""Disk usage for the Status card: what the data folder holds, what the next backup will
need, and whether there is room for it. Walking a folder of hundreds of thousands of
covers is slow, so the result is cached for five minutes."""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from . import autobackup, config, facets
from .backup import estimate_export_bytes
from .db import wal_bytes


def _dir_bytes(path: Path) -> int:
    total = 0
    stack = [path]
    while stack:
        try:
            with os.scandir(stack.pop()) as entries:
                for entry in entries:
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False):
                        total += entry.stat(follow_symlinks=False).st_size
        except OSError:
            continue
    return total


def _db_bytes(url: Any) -> int | None:
    """Size of the SQLite database (with its WAL/shm), or None for other databases."""
    if url.get_backend_name() != "sqlite" or not url.database or url.database == ":memory:":
        return None
    base = Path(url.database)
    return sum(
        p.stat().st_size for p in (base, Path(f"{base}-wal"), Path(f"{base}-shm")) if p.exists()
    )


async def collect(session: AsyncSession) -> dict[str, Any]:
    data_dir = config.SPOT_DATA_DIR
    probe = data_dir if data_dir.exists() else data_dir.parent
    usage = shutil.disk_usage(probe)
    backups = autobackup.summary()
    safety = (
        list(autobackup.backup_dir().glob("pre-migrate-*.db"))
        if autobackup.backup_dir().is_dir()
        else []
    )
    return {
        "counts_updated": facets.updated_at.isoformat() if facets.updated_at else None,
        "premigrate": {"count": len(safety), "bytes": sum(f.stat().st_size for f in safety)},
        "wal_bytes": wal_bytes(session.bind) if isinstance(session.bind, AsyncEngine) else 0,
        "database_bytes": _db_bytes(session.bind.url) if hasattr(session.bind, "url") else None,
        "covers_bytes": await asyncio.to_thread(_dir_bytes, config.IMAGES_DIR),
        "nzb_bytes": await asyncio.to_thread(_dir_bytes, config.NZB_DIR),
        "backups": backups,
        "free_bytes": usage.free,
        "total_bytes": usage.total,
        "export_estimate_bytes": await estimate_export_bytes(session),
    }


MIN_FREE = 10**9  # below 1 GB free, everything (SQLite, backups, covers) is at risk


def warning(info: dict[str, Any], keep: int) -> str | None:
    """A plain-language heads-up when the disk looks too small for the planned backups."""
    estimate, free = info["export_estimate_bytes"], info["free_bytes"]
    needed = estimate * max(keep, 1)
    if free < needed * 1.5 or free < MIN_FREE:
        return (
            f"Low disk space: about {estimate / 1e6:,.0f} MB per backup, {max(keep, 1)} kept, "
            f"and only {free / 1e9:,.1f} GB free. Lower the number of backups or add space."
        )
    return None
