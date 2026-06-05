"""Spot query service — shared by both the Newznab API and the web UI."""
from __future__ import annotations

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from .categories import expand_categories
from .models import Spot


async def search_spots(
    session: AsyncSession,
    *,
    query: str | None = None,
    categories: list[int] | None = None,
    imdb_id: str | None = None,
    offset: int = 0,
    limit: int = 100,
) -> tuple[list[Spot], int]:
    """Return (rows, total) for a search. Used by Newznab search/tvsearch/movie."""
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

    for c in conds:
        stmt = stmt.where(c)
        count_stmt = count_stmt.where(c)

    total = (await session.execute(count_stmt)).scalar_one()
    stmt = stmt.order_by(Spot.posted_at.desc()).offset(offset).limit(min(limit, 200))
    rows = list((await session.execute(stmt)).scalars().all())
    return rows, total


async def get_spot(session: AsyncSession, spot_id: int) -> Spot | None:
    return await session.get(Spot, spot_id)


async def get_spot_by_message_id(session: AsyncSession, message_id: str) -> Spot | None:
    res = await session.execute(select(Spot).where(Spot.message_id == message_id))
    return res.scalar_one_or_none()
