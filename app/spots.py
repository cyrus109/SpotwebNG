"""Spot query service — shared by both the Newznab API and the web UI."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from sqlalchemy import Integer, and_, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from .cache import cached
from .categories import expand_categories
from .filters import tree_conditions
from .models import Setting, Spot
from .spotters import blocked_condition, failed_condition

# Spotnet files erotica under the z3 genre. Most of it also lands in the 6xxx
# Newznab range, but not all — 18 spots in a real index carry z3 while sitting
# under Movies — so the category alone is not a safe test.
ADULT_CATEGORY_LOW = 6000
ADULT_CATEGORY_HIGH = 6999


def legacy_adult_condition() -> Any:
    """The adult test computed from the raw columns (also what the `adult-flag` job uses)."""
    return or_(
        Spot.category.between(ADULT_CATEGORY_LOW, ADULT_CATEGORY_HIGH),
        Spot.subcat_z.like("%z3|%"),
    )


def adult_condition() -> Any:
    """Matches every spot the adult filter is meant to hide. Uses the indexed flag; rows the
    job has not reached yet (adult < 0) fall back to the raw test, so nothing ever leaks."""
    return or_(Spot.adult == 1, and_(Spot.adult < 0, legacy_adult_condition()))


def adult_flag(category: int, subcat_z: str) -> int:
    """The flag value for a new row."""
    return int(
        ADULT_CATEGORY_LOW <= (category or 0) <= ADULT_CATEGORY_HIGH or "z3|" in (subcat_z or "")
    )


def is_adult_spot(spot: Spot) -> bool:
    """The row-level form of `adult_condition`, for single-spot routes."""
    if ADULT_CATEGORY_LOW <= (spot.category or 0) <= ADULT_CATEGORY_HIGH:
        return True
    return "z3|" in (spot.subcat_z or "")


_FTS_TOKEN = re.compile(r"[0-9A-Za-zÀ-ÿ]+")


def fts_match(query: str) -> str | None:
    """An FTS5 MATCH expression for a free-text query: every word must start a word in the
    title or description. None when a word is too short to index sensibly (use LIKE then)."""
    parts: list[str] = []
    for term in query.split():
        tokens = _FTS_TOKEN.findall(term)
        if not tokens:
            continue
        if any(len(t) < 2 for t in tokens):
            return None
        parts.extend(f'"{t}"*' for t in tokens)
    return " AND ".join(parts) or None


async def fts_enabled(session: AsyncSession) -> bool:
    """True once the SQLite FTS5 index has been built (see derived.py)."""

    async def check() -> bool:
        if session.bind.dialect.name != "sqlite":
            return False
        flag = await session.get(Setting, "fts_ready")
        return flag is not None and flag.value == "1"

    return await cached("fts-ready", 30, check)


SORTS: dict[str, Any] = {
    "new": Spot.posted_at.desc(),
    "old": Spot.posted_at.asc(),
    "size": Spot.size.desc(),
    "title": Spot.title.asc(),
    "id": Spot.id.asc(),  # insertion order: what watchlists walk
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
    exclude_adult: bool = False,
    hide_failed: bool = False,
    sort: str = "new",
    offset: int = 0,
    limit: int = 100,
    after: tuple[datetime, int] | None = None,
    min_id: int | None = None,
) -> tuple[list[Spot], int]:
    """Return (rows, total) for a search. Used by Newznab search/tvsearch/movie
    and by the web UI's Spotnet-style category tree filtering."""
    stmt = select(Spot)
    count_stmt = select(func.count()).select_from(Spot)

    conds = [blocked_condition()]  # a blocked poster is blocked everywhere
    if hide_failed:
        conds.append(failed_condition())
    if min_id is not None:
        conds.append(Spot.id > min_id)
    if query:
        match = fts_match(query) if await fts_enabled(session) else None
        if match:
            # Indexed word-prefix search: "silo s03e05" finds "Silo.S03E05.Memory".
            fts = text("SELECT rowid FROM spots_fts WHERE spots_fts MATCH :m").bindparams(m=match)
            conds.append(Spot.id.in_(fts.columns(rowid=Integer)))
        else:
            # Match every word independently. Release titles are dot-separated
            # ("Silo.S03E05.Memory") while clients send spaces ("Silo S03E05"), so a
            # single contiguous LIKE would never match.
            for term in query.split():
                like = f"%{term}%"
                conds.append(or_(Spot.title.ilike(like), Spot.description.ilike(like)))
    if imdb_id:
        conds.append(Spot.imdb_id == imdb_id.lstrip("t"))
    if categories:
        conds.append(Spot.category.in_(expand_categories(categories)))
    if exclude_adult:
        conds.append(~adult_condition())
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

    # The total is expensive on millions of rows and barely changes between page views.
    count_key = "total:" + repr(
        (
            query,
            categories,
            tree,
            imdb_id,
            min_size,
            max_size,
            since,
            exclude_adult,
            hide_failed,
            min_id,
        )
    )

    async def count() -> int:
        return (await session.execute(count_stmt)).scalar_one()

    total = await cached(count_key, None, count)  # until the next retrieval/refresh
    if after is not None and sort in ("new", "old"):
        # Keyset paging: continue strictly after the last row of the previous page. No
        # OFFSET, so page 5,000 costs the same as page 1.
        ts, last_id = after
        ahead = Spot.posted_at < ts if sort == "new" else Spot.posted_at > ts
        stmt = stmt.where(or_(ahead, and_(Spot.posted_at == ts, Spot.id < last_id)))
        offset = 0
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
