"""Facet counts for the browse sidebar and the advanced-filter drawer.

Section counts feed the left rail; option counts feed the drawer so every
choice shows how many spots it would leave — no dead ends.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import os
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from . import cache
from .config import SPOT_DATA_DIR
from .db import SessionLocal, checkpoint_after_bulk_work
from .filters import (
    _COLUMNS,
    _DIMENSIONS,
    FILTER_SECTIONS,
    base_tree,
    category_data,
    cycle_tree,
    parse_tree,
    tree_conditions,
    visible_sections,
)
from .models import Spot
from .spotters import blocked_condition

logger = logging.getLogger(__name__)

# Counting 1M+ spots on a slow disk takes minutes, so a page view must never pay for it.
# Facets are computed in the background, kept until the next retrieval/refresh replaces them,
# and saved to disk so a restart shows numbers at once.
WAIT = 3.0  # seconds a page waits for a missing facet before rendering without it
REFRESH_DELAY = 30.0  # let a retrieval's checkpoint finish and coalesce bursts of changes
MAX_KNOWN = 100
SNAPSHOT_NAME = "facets.json"

_RAW: dict[str, tuple] = {}  # name -> (function, default)
_known: dict[str, tuple[str, tuple, dict]] = {}  # every facet key a page has asked for
_pending: dict[str, asyncio.Task] = {}
_building: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "facets_building", default=None
)
_refresh_task: asyncio.Task | None = None
_refresh_again = False
_refresh_running = False
updated_at: datetime | None = None


def _key(name: str, args: tuple, kwargs: dict) -> str:
    return f"{cache.STICKY}{name}:{args}:{sorted(kwargs.items())}"


async def _build(name: str, args: tuple, kwargs: dict, scratch: dict):
    """Compute one facet in its own session. Nested facets land in `scratch`."""
    fn, _default = _RAW[name]
    token = _building.set(scratch)
    try:
        async with SessionLocal() as session:
            return await fn(session, *args, **kwargs)
    finally:
        _building.reset(token)


async def _fill(key: str, name: str, args: tuple, kwargs: dict):
    try:
        value = await _build(name, args, kwargs, {})
        cache.store(key, value)
        return value
    except Exception:
        logger.exception("facet %s failed", name)
        return None
    finally:
        _pending.pop(key, None)


def _cached_facet(name: str, default):
    """Serve a facet from memory. A missing one is built in the background (a page waits
    at most WAIT seconds for it); it is only rebuilt by refresh_facets()."""

    def decorate(fn):
        _RAW[name] = (fn, default)

        async def wrapper(session, *args, **kwargs):
            if not cache.enabled:
                return await fn(session, *args, **kwargs)
            key = _key(name, args, kwargs)
            if key not in _known:
                _known[key] = (name, args, kwargs)
                while len(_known) > MAX_KNOWN:
                    del _known[next(iter(_known))]  # forget the oldest drawer tree
            scratch = _building.get()
            if scratch is not None:  # inside a refresh: compute inline into the new set
                if key not in scratch:
                    scratch[key] = await fn(session, *args, **kwargs)
                return scratch[key]
            hit = cache.peek(key)
            if hit is not cache.MISSING:
                return hit
            task = _pending.get(key)
            if task is None:
                task = _pending[key] = asyncio.create_task(_fill(key, name, args, kwargs))
            done, _ = await asyncio.wait({task}, timeout=WAIT)
            value = task.result() if task in done else None
            return default if value is None else value

        wrapper.__name__ = fn.__name__
        wrapper.__doc__ = fn.__doc__
        return wrapper

    return decorate


def _snapshot_path():
    return SPOT_DATA_DIR / SNAPSHOT_NAME


def _save_snapshot(rows: list, stamp: datetime) -> None:
    try:
        path = _snapshot_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"updated": stamp.isoformat(), "entries": rows}))
        os.replace(tmp, path)
    except (OSError, TypeError, ValueError):
        logger.warning("could not save the facet snapshot", exc_info=True)


def load_snapshot() -> bool:
    """Fill the cache from the last refresh. False when there is none to use."""
    global updated_at
    try:
        data = json.loads(_snapshot_path().read_text())
        entries = {}
        for row in data["entries"]:
            if row["name"] not in _RAW:
                continue
            args, kwargs = tuple(row["args"]), dict(row["kwargs"])
            key = _key(row["name"], args, kwargs)
            _known[key] = (row["name"], args, kwargs)
            entries[key] = row["value"]
        stamp = datetime.fromisoformat(data["updated"])
    except (OSError, ValueError, KeyError, TypeError):
        return False
    cache.replace(cache.STICKY, entries)
    updated_at = stamp
    return bool(entries)


# What every install needs even before a page has asked for it.
def _standard() -> list[tuple[str, tuple, dict]]:
    return [("sections", (), {})] + [("dims", (hide,), {}) for hide in (False, True)]


async def refresh_facets() -> None:
    """Recount everything a page has asked for and swap the result in one step. Pages keep
    seeing the old numbers until it is done."""
    global updated_at
    for name, args, kwargs in _standard():
        _known.setdefault(_key(name, args, kwargs), (name, args, kwargs))
    fresh: dict = {}
    for key, (name, args, kwargs) in list(_known.items()):
        if key in fresh:
            continue
        try:
            fresh[key] = await _build(name, args, kwargs, fresh)
        except Exception:  # one bad key must not discard the rest: keep its old value
            logger.exception("facet %s failed to refresh", name)
            old = cache.peek(key)
            if old is not cache.MISSING:
                fresh[key] = old
    cache.replace(cache.STICKY, fresh)
    cache.invalidate("total:")  # list totals are recounted lazily, once, after a refresh
    updated_at = datetime.now(UTC)
    rows = [
        {"name": n, "args": list(a), "kwargs": k, "value": fresh[key]}
        for key, (n, a, k) in list(_known.items())
        if key in fresh
    ]
    await asyncio.to_thread(_save_snapshot, rows, updated_at)
    await checkpoint_after_bulk_work()  # a long read can have held the log back


async def _refresh_runner(delay: float) -> None:
    global _refresh_task, _refresh_again, _refresh_running
    try:
        await asyncio.sleep(delay)
        _refresh_running = True
        while True:
            _refresh_again = False
            try:
                await refresh_facets()
            except Exception:
                logger.exception("facet refresh failed")
            if not _refresh_again:
                return
    finally:
        _refresh_running = False
        _refresh_task = None


def schedule_refresh(delay: float | None = None) -> None:
    """Request a recount in the background; calls made while one is queued/running coalesce.
    An explicit delay (0 = now) pulls a queued run forward."""
    global _refresh_task, _refresh_again
    if not cache.enabled:
        return
    if _refresh_task is not None:
        if delay is not None and not _refresh_running:
            _refresh_task.cancel()  # still waiting: restart it with the shorter delay
        else:
            _refresh_again = True
            return
    try:
        _refresh_task = asyncio.get_running_loop().create_task(
            _refresh_runner(REFRESH_DELAY if delay is None else delay)
        )
    except RuntimeError:  # no event loop (sync caller): nothing to schedule on
        pass


def mark_stale() -> None:
    """The data changed (retrieval, import, prune, spotter block): drop the per-query caches
    and recount the facets. Numbers stay as they were until the recount lands."""
    cache.invalidate(keep=("total:",))  # totals stay until the recount replaces them
    schedule_refresh()


async def _count(session: AsyncSession, conds: list) -> int:
    stmt = select(func.count()).select_from(Spot).where(blocked_condition())
    for c in conds:
        stmt = stmt.where(c)
    return (await session.execute(stmt)).scalar_one()


@_cached_facet("sections", {})
async def section_counts(session: AsyncSession) -> dict[str, int]:
    """Spot count per sidebar section (All, Movies, Series, ...)."""
    counts: dict[str, int] = {}
    for section in FILTER_SECTIONS:
        key = section["key"] or "all"
        counts[key] = await _count(session, tree_conditions(section["tree"]))
    return counts


@_cached_facet("options", [])
async def option_counts(session: AsyncSession, tree: str) -> list[dict]:
    """Per-dimension options for the drawer, each with a count and state.

    A code's count is measured with the *other* dimensions still applied, so
    the number shown is what you would actually get by clicking it.
    """
    included, excluded = parse_tree(tree)
    if not included:
        return []
    cat, ztype = included[0]["cat"], included[0]["z"]
    letters = _DIMENSIONS.get((cat, ztype)) or _DIMENSIONS.get((cat, None)) or []

    data = category_data()
    names = data["codes"].get(str(cat), {})
    labels = data["labels"].get(str(cat), {})
    inc_codes = {c for g in included for codes in g["subs"].values() for c in codes}
    exc_codes = {c for g in excluded for codes in g["subs"].values() for c in codes}
    base = base_tree(tree)

    dims = []
    for letter in letters:
        # Drop this dimension from the tree so its options aren't self-limiting.
        others = ",".join(
            p for p in tree.split(",") if not p.lstrip("~!").startswith(f"{base}_{letter}")
        )
        base_conds = tree_conditions(others)
        column = _COLUMNS[letter]

        # One pass per dimension instead of one COUNT per code: the old loop
        # issued ~133 unindexable scans for Movies (measured at 1.2s a page).
        stmt = select(column, func.count()).where(blocked_condition()).group_by(column)
        for cond in base_conds:
            stmt = stmt.where(cond)
        tally: dict[str, int] = {}
        for packed, count in (await session.execute(stmt)).all():
            for code in (packed or "").split("|"):
                if code:
                    tally[code] = tally.get(code, 0) + count

        options = []
        for number, name in sorted(names.get(letter, {}).items(), key=lambda kv: int(kv[0])):
            code = f"{letter}{number}"
            state = "inc" if code in inc_codes else "exc" if code in exc_codes else ""
            total = tally.get(code, 0)
            if total or state:
                options.append(
                    {
                        "code": code,
                        "name": name,
                        "count": total,
                        "state": state,
                        "tree": cycle_tree(tree, code),
                    }
                )
        if options:
            dims.append(
                {
                    "letter": letter,
                    "label": labels.get(letter, letter.upper()),
                    "options": options,
                    "chosen": sum(1 for o in options if o["state"]),
                }
            )
    return dims


@_cached_facet("dims", [])
async def section_dimensions(session: AsyncSession, hide_adult: bool = False) -> list[dict]:
    """Every category's dimensions, for refining from the "All" view.

    "All" spans four head categories whose sub-dimension letters mean different
    things (`c` is Language for video but Bitrate for audio), so there is no
    single coherent list — offer them grouped by category instead, and let each
    option carry the tree that both applies the filter and enters that section.
    """
    groups = []
    for section in visible_sections(hide_adult):
        if not section["tree"]:
            continue  # "All" is the thing we're refining *from*
        dims = await option_counts(session, section["tree"])
        if dims:
            groups.append(
                {
                    "key": section["key"],
                    "label": section["label"],
                    "tree": section["tree"],
                    "dimensions": dims,
                    "option_total": sum(len(d["options"]) for d in dims),
                }
            )
    return groups
