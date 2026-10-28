"""A tiny in-process TTL cache for expensive read-only aggregates (counts, stats).

One process, one worker: no locking. Keys can contain user-supplied search text, so the
cache is bounded three ways: a hard cap on entries (oldest evicted first), expired entries
purged on insert, and oversized keys are simply not cached. `invalidate()` is called after
a retrieval commits new rows so cached counts don't lag behind it.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

STICKY = "facet:"  # sidebar/drawer counts: survive invalidate(), swapped by facets.refresh_facets()
MISSING = object()
MAX_ENTRIES = 1000
MAX_STICKY = 200  # request-chosen drawer trees must not grow the no-expiry set without bound
MAX_KEY_LENGTH = 512

_store: dict[str, tuple[float, Any]] = {}  # insertion-ordered: the first key is the oldest
enabled = True  # tests switch it off so state changes are always visible


async def cached(key: str, ttl: float | None, factory: Callable[[], Awaitable[Any]]) -> Any:
    """Memoise `factory()`; ttl=None keeps the value until invalidate() or replace()."""
    if not enabled or len(key) > MAX_KEY_LENGTH:
        return await factory()
    now = time.monotonic()
    hit = _store.get(key)
    if hit and hit[0] > now:
        return hit[1]
    value = await factory()
    if len(_store) >= MAX_ENTRIES:
        for stale in [k for k, (expires, _v) in _store.items() if expires <= now]:
            del _store[stale]
        for oldest in [k for k in _store if not k.startswith(STICKY)]:
            if len(_store) < MAX_ENTRIES:
                break
            del _store[oldest]  # evict the oldest; the facet counts are never evicted
    _store.pop(key, None)  # re-insert at the end
    _store[key] = (float("inf") if ttl is None else now + ttl, value)
    return value


def peek(key: str) -> Any:
    hit = _store.get(key)
    return hit[1] if hit and hit[0] > time.monotonic() else MISSING


def store(key: str, value: Any) -> None:
    if len(key) > MAX_KEY_LENGTH:
        return
    _store.pop(key, None)
    sticky = [k for k in _store if k.startswith(STICKY)]
    if key.startswith(STICKY) and len(sticky) >= MAX_STICKY:
        for oldest in sticky[: len(sticky) - MAX_STICKY + 1]:
            del _store[oldest]
    _store[key] = (float("inf"), value)


def replace(prefix: str, entries: dict[str, Any]) -> None:
    """Swap every `prefix` entry for `entries` in one step, so readers never see a gap."""
    for key in [k for k in _store if k.startswith(prefix)]:
        del _store[key]
    for key, value in entries.items():
        store(key, value)


def invalidate(prefix: str = "", keep: tuple[str, ...] = ()) -> None:
    """Drop cached values. The sticky facet counts are kept (only a refresh replaces them)
    unless `prefix` asks for them explicitly, and anything starting with a `keep` prefix."""
    keep_sticky = not prefix.startswith(STICKY)
    for key in [k for k in _store if k.startswith(prefix)]:
        if (keep_sticky and key.startswith(STICKY)) or key.startswith(keep):
            continue
        del _store[key]
