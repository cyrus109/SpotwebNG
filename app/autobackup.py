"""Automatic backups: a nightly export (the same file Settings -> Export produces)
written to <data>/backups, keeping the newest N. With the data volume intact a
rebuilt server never needs it; it exists for the day the volume is lost."""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from . import config
from .backup import export_chunks
from .settings_store import load_settings

logger = logging.getLogger("spotwebng")
PATTERN = "spotwebng-*.jsonl.gz"
CHECK_EVERY = 30 * 60  # seconds
DUE_AFTER = timedelta(hours=23)


def backup_dir() -> Path:
    return config.SPOT_DATA_DIR / "backups"


def list_backups(directory: Path | None = None) -> list[Path]:
    directory = directory or backup_dir()
    return sorted(directory.glob(PATTERN)) if directory.is_dir() else []


def summary(directory: Path | None = None) -> dict:
    files = list_backups(directory)
    latest = max((f.stat().st_mtime for f in files), default=None)
    return {
        "count": len(files),
        "bytes": sum(f.stat().st_size for f in files),
        "latest": datetime.fromtimestamp(latest, UTC).isoformat() if latest else None,
    }


async def make_backup(
    factory: async_sessionmaker[AsyncSession], directory: Path | None = None
) -> Path:
    """Write a full export; the file only appears under its final name once complete."""
    directory = directory or backup_dir()
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    final = directory / f"spotwebng-{stamp}.jsonl.gz"
    partial = final.with_suffix(".part")
    try:
        with open(partial, "wb") as fh:
            async for chunk in export_chunks(factory):
                await asyncio.to_thread(fh.write, chunk)
        os.replace(partial, final)
    finally:
        partial.unlink(missing_ok=True)
    return final


def prune(keep: int, directory: Path | None = None) -> int:
    """Delete all but the newest `keep` backups; returns how many were removed."""
    files = list_backups(directory)
    doomed = files[: max(len(files) - keep, 0)]
    for path in doomed:
        path.unlink(missing_ok=True)
    return len(doomed)


async def backup_if_due(factory: async_sessionmaker[AsyncSession]) -> Path | None:
    async with factory() as session:
        keep = (await load_settings(session))["backup_keep"]
    if keep <= 0:
        return None
    files = list_backups()
    if (
        files
        and datetime.now(UTC) - datetime.fromtimestamp(files[-1].stat().st_mtime, UTC) < DUE_AFTER
    ):
        return None
    made = await make_backup(factory)
    prune(keep)
    logger.info("automatic backup written: %s", made.name)
    return made


async def backup_loop(factory: async_sessionmaker[AsyncSession]) -> None:
    await asyncio.sleep(120)  # let start-up and the first retrieval settle
    while True:
        try:
            await backup_if_due(factory)
        except Exception:
            logger.exception("automatic backup failed")
        await asyncio.sleep(CHECK_EVERY)
