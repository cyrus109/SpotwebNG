"""Spot query service — shared by both the Newznab API and the web UI."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from .categories import expand_categories
from .filters import tree_conditions
from .models import Spot

SORTS: dict[str, Any] = {
    "new": Spot.posted_at.desc(),
    "old": Spot.posted_at.asc(),
    "size": Spot.size.desc(),
    "title": Spot.title.asc(),
}


async def search_spots(
    session: AsyncSession,
    *,
    query: str | None = None,
    categories: list[int] | None = None,
    tree: str | None = None,
    imdb_id: str | None = None,
    min_size: int | None = None,
    max_size: int | None = None,
    since: datetime | None = None,
    sort: str = "new",
    offset: int = 0,
    limit: int = 100,
) -> tuple[list[Spot], int]:
    """Return (rows, total) for a search. Used by Newznab search/tvsearch/movie
    and by the web UI's Spotnet-style category tree filtering."""
    stmt = select(Spot)
    count_stmt = select(func.count()).select_from(Spot)

    conds = []
    if query:
        like = f"%{query}%"
        conds.append(or_(Spot.title.ilike(like), Spot.description.ilike(like)))
    if imdb_id:
        conds.append(Spot.imdb_id == imdb_id.lstrip("t"))
    if categories:
        conds.append(Spot.category.in_(expand_categories(categories)))
    if tree:
        conds.extend(tree_conditions(tree))
    if min_size:
        conds.append(Spot.size >= min_size)
    if max_size:
        conds.append(Spot.size <= max_size)
    if since:
        conds.append(Spot.posted_at >= since)

    for c in conds:
        stmt = stmt.where(c)
        count_stmt = count_stmt.where(c)

    total = (await session.execute(count_stmt)).scalar_one()
    # min() alone let limit=-1 through, which SQLite reads as "no limit".
    safe_limit = max(1, min(limit, 200))
    safe_offset = max(0, offset)
    # id is the tiebreaker: posted_at is not unique, so without it rows can
    # repeat or vanish between pages.
    stmt = (
        stmt.order_by(SORTS.get(sort, SORTS["new"]), Spot.id.desc())
        .offset(safe_offset)
        .limit(safe_limit)
    )
    rows = list((await session.execute(stmt)).scalars().all())
    return rows, total


async def get_spot(session: AsyncSession, spot_id: int) -> Spot | None:
    return await session.get(Spot, spot_id)


async def get_spot_by_message_id(session: AsyncSession, message_id: str) -> Spot | None:
    res = await session.execute(select(Spot).where(Spot.message_id == message_id))
    return res.scalar_one_or_none()
