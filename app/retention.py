"""Retention — keeps the database from growing without bound.

Two modes, matching Spotweb:
  fullonly    keep the spot row (still searchable) but drop the heavy parts:
              the cached image, the NZB segment list and the long description.
  everything  delete spots older than the cutoff outright.

`enriched = 3` marks a stripped spot so the enrichment loop never re-fetches it.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import IMAGES_DIR, NZB_DIR
from .models import Comment, Spot
from .settings_store import load_settings

PRUNED = 3
CHUNK = 2000


def _remove_artifacts_many(spot_ids: list[int]) -> None:
    """Unlink off the event loop — tens of thousands of syscalls would block it."""
    for spot_id in spot_ids:
        _remove_artifacts(spot_id)


def _remove_artifacts(spot_id: int) -> None:
    for path in (IMAGES_DIR / f"{spot_id}.jpg", NZB_DIR / f"{spot_id}.nzb"):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def retention_cutoff(settings: dict, now: datetime | None = None) -> datetime | None:
    """Oldest `posted_at` that survives pruning; None when nothing is deleted."""
    if settings["retention_days"] and settings["retention_type"] == "everything":
        return (now or datetime.now(UTC)) - timedelta(days=settings["retention_days"])
    return None


async def prune_old_spots(session: AsyncSession, now: datetime | None = None) -> dict:
    """Apply the configured retention policy. Safe to run repeatedly."""
    settings = await load_settings(session)
    days = settings["retention_days"]
    if not days:
        return {"deleted": 0, "stripped": 0, "cutoff": None}

    now = now or datetime.now(UTC)
    cutoff = now - timedelta(days=days)

    if settings["retention_type"] == "everything":
        doomed = (
            (await session.execute(select(Spot.id).where(Spot.posted_at < cutoff))).scalars().all()
        )
        # Both SQLite and Postgres cap bind parameters near 32k, so an IN() over
        # the whole doomed set fails outright once retention has real work.
        for start in range(0, len(doomed), CHUNK):
            batch = doomed[start : start + CHUNK]
            await asyncio.to_thread(_remove_artifacts_many, list(batch))
            await session.execute(delete(Spot).where(Spot.id.in_(batch)))
            await session.commit()
        await session.execute(delete(Comment).where(Comment.posted_at < cutoff))
        await session.commit()
        return {"deleted": len(doomed), "stripped": 0, "cutoff": cutoff.isoformat()}

    stripped = 0
    while True:
        stale = (
            (
                await session.execute(
                    select(Spot)
                    .where(Spot.posted_at < cutoff, Spot.enriched != PRUNED)
                    .limit(CHUNK)
                )
            )
            .scalars()
            .all()
        )
        if not stale:
            break
        await asyncio.to_thread(_remove_artifacts_many, [s.id for s in stale])
        for spot in stale:
            spot.poster_url = ""
            spot.nzb_segments = ""
            spot.description = ""
            spot.enriched = PRUNED
        stripped += len(stale)
        await session.commit()
    return {"deleted": 0, "stripped": stripped, "cutoff": cutoff.isoformat()}
