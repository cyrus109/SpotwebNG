"""Duplicate grouping: the same title posted in several qualities or by several posters.

Spots share a `title_key` (see titles.py). The grid shows "N releases" on a card and the
spot page lists the other releases with a suggested best one (highest resolution, then the
better source, then the larger file)."""

from __future__ import annotations

import re

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Spot
from .spots import adult_condition
from .spotters import blocked_condition

_RESOLUTION = [
    (r"2160p|\b4k\b|\buhd\b", 40),
    (r"1080[pi]", 30),
    (r"\b720p", 20),
    (r"576p|480p|\bdvd", 10),
]
_SOURCE = [
    (r"remux", 5),
    (r"blu-?ray|bdrip|brrip", 4),
    (r"web-?dl", 3),
    (r"webrip", 2),
    (r"hdtv", 1),
]


def quality_score(title: str) -> int:
    """A rough release-quality rank from the words in the title (higher is better)."""
    low = title.lower()
    score = 0
    for pattern, points in _RESOLUTION:
        if re.search(pattern, low):
            score += points
            break
    for pattern, points in _SOURCE:
        if re.search(pattern, low):
            score += points
            break
    return score


def best(spots: list[Spot]) -> Spot | None:
    return max(spots, key=lambda s: (quality_score(s.title), s.size or 0), default=None)


def _visible(hide_adult: bool) -> list:
    conds = [blocked_condition()]
    if hide_adult:
        conds.append(~adult_condition())
    return conds


async def release_counts(
    session: AsyncSession, rows: list[Spot], hide_adult: bool
) -> dict[str, int]:
    """title_key -> number of visible releases, for keys with more than one."""
    keys = {r.title_key for r in rows if r.title_key}
    if not keys:
        return {}
    stmt = (
        select(Spot.title_key, func.count())
        .where(Spot.title_key.in_(keys))
        .group_by(Spot.title_key)
    )
    for cond in _visible(hide_adult):
        stmt = stmt.where(cond)
    return {key: n for key, n in (await session.execute(stmt)).all() if n > 1}


async def siblings(
    session: AsyncSession, spot: Spot, hide_adult: bool, limit: int = 15
) -> list[Spot]:
    """The other visible releases of the same title, newest first."""
    if not spot.title_key:
        return []
    stmt = select(Spot).where(Spot.title_key == spot.title_key, Spot.id != spot.id)
    for cond in _visible(hide_adult):
        stmt = stmt.where(cond)
    return list(
        (await session.execute(stmt.order_by(Spot.posted_at.desc()).limit(limit))).scalars().all()
    )
