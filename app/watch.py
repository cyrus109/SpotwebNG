"""Watchlists: saved searches that act on *new* matching spots.

After every retrieval each enabled watch looks at spots stored since it last ran (by id),
restricted to recently posted ones so a history walk does not set them off. A match is
skipped if it was already downloaded or already handled by this watch; otherwise the watch
notifies, or sends it to the downloader (which applies the availability policy). Adult
content stays hidden when the admin hid it, and blocked posters never match.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .categories import CATEGORY_NAMES
from .downloader import send_spot
from .downloads import downloaded_map
from .models import Spot, Watch, WatchHit
from .notify import channels, notify
from .settings_store import load_settings
from .spots import search_spots

logger = logging.getLogger("spotwebng")

MAX_AGE = timedelta(days=14)  # older spots are history, not news
PER_RUN = 25  # matches handled per watch per run (the rest wait for the next run)
ACTIONS = ("notify", "send")


async def _max_spot_id(session: AsyncSession) -> int:
    return (await session.execute(select(func.max(Spot.id)))).scalar_one() or 0


async def new_watch(session: AsyncSession, **fields: Any) -> Watch:
    """Create a watch that starts from *now*: existing spots are not acted upon."""
    watch = Watch(**fields, last_id=await _max_spot_id(session))
    session.add(watch)
    await session.commit()
    return watch


def _describe(spot: Spot) -> str:
    gb = (spot.size or 0) / 1024**3
    return f"{CATEGORY_NAMES.get(spot.category, 'Other')} · {gb:.1f} GB"


async def _already_handled(session: AsyncSession, watch_id: int, message_id: str) -> bool:
    return (
        await session.execute(
            select(WatchHit.id).where(
                WatchHit.watch_id == watch_id, WatchHit.message_id == message_id
            )
        )
    ).first() is not None


async def run_watches(session: AsyncSession) -> dict[str, int]:
    """Evaluate every enabled watch against spots stored since it last ran."""
    prefs = await load_settings(session)
    watches = (
        (await session.execute(select(Watch).where(Watch.enabled == 1).order_by(Watch.id)))
        .scalars()
        .all()
    )
    totals = {"watches": len(watches), "matched": 0, "sent": 0, "notified": 0, "skipped": 0}
    now = datetime.now(UTC)
    for watch in watches:
        newest = await _max_spot_id(session)  # read first: spots arriving meanwhile wait a run
        if watch.last_id < 0:  # imported from another install: start from now
            watch.last_id = newest
            await session.commit()
            continue
        rows, _ = await search_spots(
            session,
            query=watch.query or None,
            tree=watch.tree or None,
            min_size=watch.min_size or None,
            max_size=watch.max_size or None,
            since=now - MAX_AGE,
            exclude_adult=bool(prefs["hide_adult"]),
            hide_failed=bool(prefs["hide_failed_signatures"]),
            min_id=watch.last_id,
            sort="id",
            limit=PER_RUN,
        )
        downloaded = await downloaded_map(session, [s.message_id for s in rows])
        for spot in rows:
            totals["matched"] += 1
            if await _already_handled(session, watch.id, spot.message_id):
                continue
            outcome = await _act(session, prefs, watch, spot, spot.message_id in downloaded, totals)
            session.add(
                WatchHit(
                    watch_id=watch.id,
                    message_id=spot.message_id,
                    title=spot.title[:512],
                    outcome=outcome[:120],
                )
            )
            watch.hits += 1
        # Everything up to `newest` has been seen unless the batch was full.
        watch.last_id = rows[-1].id if len(rows) >= PER_RUN else max(newest, watch.last_id)
        watch.last_run_at = now
        await session.commit()
    return totals


async def _act(
    session: AsyncSession,
    prefs: dict[str, Any],
    watch: Watch,
    spot: Spot,
    downloaded: bool,
    totals: dict[str, int],
) -> str:
    if downloaded:
        totals["skipped"] += 1
        return "skipped: already downloaded"
    link = f"{prefs['public_url']}/spot/{spot.id}" if prefs["public_url"] else ""
    title = f"{watch.name}: {spot.title}"
    if watch.action == "send" and prefs["nzb_action"] in ("sabnzbd", "blackhole"):
        result = await send_spot(session, spot, prefs)
        if result["ok"]:
            totals["sent"] += 1
            outcome = f"sent: {result['message']}"
        else:
            outcome = f"not sent: {result['message']}"
        if channels(prefs):
            await notify(prefs, title, f"{_describe(spot)}\n{outcome}", link)
        return outcome
    totals["notified"] += 1
    if channels(prefs):
        await notify(prefs, title, f"New match · {_describe(spot)}", link)
        return "notified"
    return "matched (no notification channel configured)"
