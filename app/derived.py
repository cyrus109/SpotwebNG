"""Derived-data jobs: values computed from what is already stored (see jobs.py).

Each one also has an ingest-time counterpart, so only rows stored before the feature
existed ever need the job.
"""

from __future__ import annotations

from sqlalchemy import case, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from .jobs import register
from .models import Setting, Spot
from .spots import legacy_adult_condition
from .titles import title_key

BATCH = 5000


@register("adult-flag", 1, "Flagging adult content")
async def _adult(session: AsyncSession) -> int:
    ids = select(Spot.id).where(Spot.adult < 0).limit(BATCH)
    result = await session.execute(
        update(Spot)
        .where(Spot.id.in_(ids))
        .values(adult=case((legacy_adult_condition(), 1), else_=0))
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    return result.rowcount or 0


@register("title-keys", 1, "Grouping duplicate releases")
async def _title_keys(session: AsyncSession) -> int:
    rows = (
        await session.execute(select(Spot.id, Spot.title).where(Spot.title_key == "").limit(2000))
    ).all()
    if rows:
        await session.execute(
            update(Spot),
            [
                {"id": spot_id, "title_key": title_key(title) or title.lower()[:255] or "-"}
                for spot_id, title in rows
            ],
        )
    await session.commit()
    return len(rows)


FTS_READY = "fts_ready"


@register("search-index", 1, "Building the search index")
async def _search_index(session: AsyncSession) -> int:
    """SQLite only: build the FTS5 index over existing spots, then keep it in sync."""
    bind = await session.connection()
    if bind.dialect.name != "sqlite":
        return 0
    exists = (
        await session.execute(text("SELECT 1 FROM sqlite_master WHERE name = 'spots_fts'"))
    ).first()
    if exists is None:
        return 0  # SQLite without FTS5
    flag = await session.get(Setting, FTS_READY)
    if flag is not None and flag.value == "1":
        return 0
    # One transaction: rebuild from the content table, then add the UPDATE/DELETE
    # triggers, so no change can slip between the two.
    await session.execute(text("INSERT INTO spots_fts(spots_fts) VALUES('rebuild')"))
    for stmt in (
        "CREATE TRIGGER IF NOT EXISTS spots_fts_ad AFTER DELETE ON spots BEGIN "
        "INSERT INTO spots_fts(spots_fts, rowid, title, description) "
        "VALUES ('delete', old.id, old.title, old.description); END",
        "CREATE TRIGGER IF NOT EXISTS spots_fts_au AFTER UPDATE OF title, description ON spots BEGIN "
        "INSERT INTO spots_fts(spots_fts, rowid, title, description) "
        "VALUES ('delete', old.id, old.title, old.description); "
        "INSERT INTO spots_fts(rowid, title, description) "
        "VALUES (new.id, new.title, new.description); END",
    ):
        await session.execute(text(stmt))
    if flag is None:
        session.add(Setting(key=FTS_READY, value="1"))
    else:
        flag.value = "1"
    await session.commit()
    return 1
