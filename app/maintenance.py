"""Housekeeping commands for the index: clean up, reset, or start again from zero.

All of them run under RETRIEVE_LOCK (no retrieval or import in flight) and none
touches settings, users, sessions or API keys. `wipe` is the only one that
removes spots, and the route demands a typed confirmation for it.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import IMAGES_DIR, NZB_DIR
from .models import Comment, Setting, Spot, UsenetState
from .progress import KEY as STATUS_KEY
from .retention import prune_old_spots

ACTIONS = ("prune", "orphans", "vacuum", "reset-bookmarks", "wipe-comments", "wipe")
DESTRUCTIVE = ("wipe-comments", "wipe")


def _unlink_dir(directory: Path, keep_ids: set[int] | None) -> int:
    """Delete cached files named <id>.<ext>; with keep_ids, only those not in it."""
    removed = 0
    if not directory.is_dir():
        return 0
    for path in directory.iterdir():
        if not path.is_file():
            continue
        if keep_ids is not None and path.stem.isdigit() and int(path.stem) in keep_ids:
            continue
        try:
            path.unlink()
            removed += 1
        except OSError:
            pass
    return removed


async def run_maintenance(session: AsyncSession, action: str) -> str:
    """Run one action and return a human-readable result. Raises ValueError for
    an unknown action."""
    if action == "prune":
        result = await prune_old_spots(session)
        if result["cutoff"] is None:
            return "Retention is set to keep everything; nothing to do"
        return f"Removed {result['deleted']:,} spots, stripped {result['stripped']:,}"

    if action == "orphans":
        ids = set((await session.execute(select(Spot.id))).scalars().all())
        spot_mids = select(Spot.message_id)
        res = await session.execute(delete(Comment).where(~Comment.spot_message_id.in_(spot_mids)))
        await session.commit()
        files = await asyncio.to_thread(_unlink_dir, IMAGES_DIR, ids)
        files += await asyncio.to_thread(_unlink_dir, NZB_DIR, ids)
        return f"Removed {res.rowcount or 0:,} orphan comments and {files:,} stale cached files"

    if action == "vacuum":
        await session.commit()  # end any open transaction: VACUUM can't run inside one
        engine = session.bind
        async with engine.connect() as conn:
            conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
            await conn.exec_driver_sql("VACUUM")
        return "Database compacted"

    if action == "reset-bookmarks":
        await session.execute(delete(UsenetState))
        await session.execute(delete(Setting).where(Setting.key == STATUS_KEY))
        await session.commit()
        return "Retrieval position reset; the next run starts from the backfill window again"

    if action == "wipe-comments":
        res = await session.execute(delete(Comment))
        await session.execute(delete(UsenetState).where(UsenetState.infotype == "comments"))
        await session.commit()
        return f"Deleted {res.rowcount or 0:,} comments"

    if action == "wipe":
        spots = await session.execute(delete(Spot))
        await session.execute(delete(Comment))
        await session.execute(delete(UsenetState))
        await session.execute(delete(Setting).where(Setting.key == STATUS_KEY))
        await session.commit()
        files = await asyncio.to_thread(_unlink_dir, IMAGES_DIR, None)
        files += await asyncio.to_thread(_unlink_dir, NZB_DIR, None)
        return (
            f"Deleted {spots.rowcount or 0:,} spots and {files:,} cached files; starting from zero"
        )

    raise ValueError(f"Unknown action: {action}")
