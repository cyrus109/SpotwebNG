"""Posters (spotters): block or trust by key, and keep their signatures honest.

A *blocked* spotter's spots disappear from every listing, search, facet count and
Newznab response. A spotter is identified by `spotter_id`, derived from the key a
spot was verifiably signed with, so a block survives a name change.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from . import signing
from .cache import invalidate
from .jobs import register
from .models import Spot, Spotter

STATUSES = ("neutral", "trusted", "blocked")


def blocked_condition() -> Any:
    """SQL condition: the spot's poster is not blocked."""
    return ~exists(
        select(1).where(
            Spotter.status == "blocked",
            or_(
                Spotter.spotter_id == Spot.spotter_id,
                # a Spotweb-style short id (imported blacklist) blocks every id it prefixes
                and_(
                    func.length(Spotter.spotter_id) <= 8,
                    Spot.spotter_id.startswith(Spotter.spotter_id),
                ),
            ),
        )
    )


def failed_condition() -> Any:
    """SQL condition: the spot's signature did not fail verification."""
    return Spot.verified != signing.FAILED


async def set_status(session: AsyncSession, spotter_id: str, status: str, note: str = "") -> None:
    if status not in STATUSES or not spotter_id or len(spotter_id) > 16:
        raise ValueError("bad spotter status")
    row = await session.get(Spotter, spotter_id)
    if status == "neutral" and not note:
        if row is not None:
            await session.delete(row)
    elif row is None:
        session.add(Spotter(spotter_id=spotter_id, status=status, note=note[:255]))
    else:
        row.status, row.note = status, note[:255]
    await session.commit()
    invalidate()


async def status_of(session: AsyncSession, spotter_id: str) -> str:
    if not spotter_id:
        return "neutral"
    row = await session.get(Spotter, spotter_id)
    return row.status if row else "neutral"


async def listing(session: AsyncSession) -> list[dict]:
    """Spotters with a verdict, with how many spots and a sample name each."""
    rows = (
        (await session.execute(select(Spotter).order_by(Spotter.status, Spotter.spotter_id)))
        .scalars()
        .all()
    )
    out = []
    for row in rows:
        count, name = (
            await session.execute(
                select(func.count(), func.min(Spot.poster)).where(Spot.spotter_id == row.spotter_id)
            )
        ).one()
        out.append(
            {
                "spotter_id": row.spotter_id,
                "status": row.status,
                "note": row.note,
                "spots": count,
                "name": name or "",
            }
        )
    return out


@register("verify-signatures", 1, "Checking poster signatures")
async def _verify_stored(session: AsyncSession) -> int:
    """Evaluate spots that were stored with their raw header but never verified."""
    rows = (
        (
            await session.execute(
                select(Spot).where(Spot.verified == 0, Spot.from_hdr != "").limit(2000)
            )
        )
        .scalars()
        .all()
    )
    for spot in rows:
        spot.verified, spot.keyid, sid = signing.verify_spot(
            spot.from_hdr, spot.title, spot.message_id.strip("<>")
        )
        spot.spotter_id = sid[:16]
    await session.commit()
    return len(rows)
