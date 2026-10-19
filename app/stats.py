"""Numbers for the Stats page: spots per day, downloads per day, top posters, category mix.

Aggregates over the whole index are not cheap, so the page caches them for five minutes.
Adult content is left out when the admin hides it; blocked posters never count.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .categories import CATEGORY_NAMES
from .models import Comment, Download, Spot
from .spots import adult_condition
from .spotters import blocked_condition

DAYS = 90


def _days_back(now: datetime) -> list[str]:
    return [(now - timedelta(days=i)).date().isoformat() for i in range(DAYS - 1, -1, -1)]


def series(counts: dict[str, int], days: list[str]) -> list[int]:
    """One value per day, zero-filled."""
    return [counts.get(day, 0) for day in days]


def bars(values: list[int], width: int = 720, height: int = 120) -> list[dict[str, float]]:
    """Rectangles for an inline-SVG bar chart (scaled to the tallest bar)."""
    peak = max(values, default=0) or 1
    slot = width / max(len(values), 1)
    out = []
    for i, value in enumerate(values):
        h = round(value / peak * height, 1)
        out.append(
            {
                "x": round(i * slot, 1),
                "y": round(height - h, 1),
                "w": round(max(slot - 1, 1), 1),
                "h": h,
                "v": value,
            }
        )
    return out


async def collect(session: AsyncSession, hide_adult: bool) -> dict[str, Any]:
    now = datetime.now(UTC)
    days = _days_back(now)
    since = now - timedelta(days=DAYS)
    visible = [blocked_condition()] + ([~adult_condition()] if hide_adult else [])

    def scoped(stmt):
        for cond in visible:
            stmt = stmt.where(cond)
        return stmt

    spot_total = (
        await session.execute(scoped(select(func.count()).select_from(Spot)))
    ).scalar_one()
    per_day = dict(
        (str(day), n)
        for day, n in (
            await session.execute(
                scoped(
                    select(func.date(Spot.posted_at), func.count()).where(Spot.posted_at >= since)
                ).group_by(func.date(Spot.posted_at))
            )
        ).all()
    )
    downloads_per_day = dict(
        (str(day), n)
        for day, n in (
            await session.execute(
                select(func.date(Download.last_at), func.count())
                .where(Download.last_at >= since)
                .group_by(func.date(Download.last_at))
            )
        ).all()
    )
    posters = (
        await session.execute(
            scoped(select(Spot.poster, func.count().label("n")).where(Spot.poster != ""))
            .group_by(Spot.poster)
            .order_by(func.count().desc())
            .limit(10)
        )
    ).all()
    categories: dict[str, int] = {}
    for category, n in (
        await session.execute(scoped(select(Spot.category, func.count())).group_by(Spot.category))
    ).all():
        parent = CATEGORY_NAMES.get((category // 1000) * 1000, "Other")
        categories[parent] = categories.get(parent, 0) + n
    return {
        "spots": spot_total,
        "comments": (await session.execute(select(func.count()).select_from(Comment))).scalar_one(),
        "downloads": (
            await session.execute(select(func.count()).select_from(Download))
        ).scalar_one(),
        "days": days,
        "spots_per_day": series(per_day, days),
        "downloads_per_day": series(downloads_per_day, days),
        "top_posters": [(name, n) for name, n in posters],
        "categories": sorted(categories.items(), key=lambda kv: -kv[1]),
        "generated": now.isoformat(),
    }
