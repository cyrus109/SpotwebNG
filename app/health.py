"""Spot health: are the articles an NZB points at still on the news server?

Old Usenet posts lose articles to retention and takedowns, and a download of a spot with
missing articles fails late. We assemble the spot's NZB (cached), take a sample of its
real segment message-ids (first, last, and random ones, at most SAMPLE) and ask the
server with STAT, which transfers no article data. Several connections work in parallel.

    complete (1)  every sampled article is present
    partial  (2)  some are missing but at least half are present (par2 might repair it)
    dead     (3)  fewer than half are present
"""

from __future__ import annotations

import asyncio
import random
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from .models import Spot
from .retriever import NntpClient, NntpError, _connections, _load_provider, _open_extras

UNCHECKED, COMPLETE, PARTIAL, DEAD = 0, 1, 2, 3
LABELS = {UNCHECKED: "unchecked", COMPLETE: "complete", PARTIAL: "partial", DEAD: "dead"}
SAMPLE = 30
MAX_SEGMENTS_CONSIDERED = 100_000
RECHECK_AFTER = timedelta(hours=24)
_SEGMENT = re.compile(rb"<segment\b[^>]*>\s*([^<>\s]{1,255})\s*</segment>")


def nzb_segment_ids(nzb: bytes) -> list[str]:
    """Message-ids of every <segment> in an NZB. A regex, not an XML parser: the NZB comes
    from Usenet, and a bounded scan is immune to entity-expansion tricks."""
    ids = []
    for match in _SEGMENT.finditer(nzb):
        ids.append(match.group(1).decode("utf-8", "replace"))
        if len(ids) >= MAX_SEGMENTS_CONSIDERED:
            break
    return ids


def pick_sample(ids: list[str], size: int = SAMPLE, rng: random.Random | None = None) -> list[str]:
    """First and last article plus a random selection of the rest."""
    if len(ids) <= size:
        return list(ids)
    rng = rng or random.Random()
    middle = rng.sample(ids[1:-1], size - 2)
    return [ids[0], *middle, ids[-1]]


def classify(present: int, sampled: int) -> tuple[int, int]:
    """(status, percentage present) for a sample."""
    if sampled <= 0:
        return UNCHECKED, -1
    pct = round(100 * present / sampled)
    if present == sampled:
        return COMPLETE, 100
    return (PARTIAL if pct >= 50 else DEAD), pct


async def _stat_all(provider: dict, ids: list[str]) -> tuple[int, int]:
    """(present, answered) for `ids`, spread over the provider's connections."""
    client = NntpClient()
    await client.connect(
        provider["provider_host"],
        int(provider.get("provider_port", 119)),
        provider.get("provider_ssl", "yes") == "yes",
        provider.get("username", ""),
        provider.get("password", ""),
    )
    workers = min(_connections(provider, 4), len(ids))
    extras = await _open_extras(provider, workers - 1, group=None)
    pool = [client, *extras]
    try:

        async def run(conn: NntpClient, chunk: list[str]) -> list[bool | None]:
            return [await conn.stat(mid) for mid in chunk]

        chunks = [ids[i :: len(pool)] for i in range(len(pool))]
        results = await asyncio.gather(*(run(c, ch) for c, ch in zip(pool, chunks, strict=True)))
    finally:
        for extra in extras:
            await extra.quit()
        await client.quit()
    flat = [
        r for chunk in results for r in chunk if r is not None
    ]  # inconclusive answers are skipped
    return sum(1 for r in flat if r), len(flat)


_inflight: set[int] = set()  # spots being checked right now: one check at a time per spot
_slots = asyncio.Semaphore(3)  # and at most three spots at once, whoever asks


async def check_spot(session: AsyncSession, spot: Spot, force: bool = False) -> dict[str, Any]:
    """Check (or reuse a recent check of) a spot. Returns {health, label, pct, checked_at, ...}."""
    now = datetime.now(UTC)
    fresh = spot.health_checked_at and now - _aware(spot.health_checked_at) < RECHECK_AFTER
    if spot.health != UNCHECKED and fresh and not force:
        return _report(spot)
    if spot.id in _inflight:
        return {**_report(spot), "error": "A check of this spot is already running"}
    _inflight.add(spot.id)
    try:
        async with _slots:
            return await _check(session, spot, now)
    finally:
        _inflight.discard(spot.id)


async def _check(session: AsyncSession, spot: Spot, now: datetime) -> dict[str, Any]:
    from .newznab import _real_nzb  # assembles (and caches) the real NZB

    nzb = await _real_nzb(session, spot)
    if not nzb:
        return {
            **_report(spot),
            "error": "The spot's NZB is not available, so it cannot be checked",
        }
    ids = nzb_segment_ids(nzb)
    if not ids:
        return {**_report(spot), "error": "The NZB lists no articles"}
    sample = pick_sample(ids)
    try:
        present, answered = await _stat_all(await _load_provider(session), sample)
    except (NntpError, OSError, TimeoutError) as exc:
        return {**_report(spot), "error": f"Could not reach the news server ({type(exc).__name__})"}
    if answered == 0:
        return {**_report(spot), "error": "The news server gave no usable answers"}
    spot.health, spot.health_pct = classify(present, answered)
    spot.health_checked_at = now
    await session.commit()
    return {**_report(spot), "sampled": answered, "articles": len(ids)}


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _report(spot: Spot) -> dict[str, Any]:
    return {
        "health": spot.health,
        "label": LABELS.get(spot.health, "unchecked"),
        "pct": spot.health_pct,
        "checked_at": spot.health_checked_at.isoformat() if spot.health_checked_at else None,
    }
