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

MAX_ENTRIES = 1000
MAX_KEY_LENGTH = 512

_store: dict[str, tuple[float, Any]] = {}  # insertion-ordered: the first key is the oldest
enabled = True  # tests switch it off so state changes are always visible


async def cached(key: str, ttl: float, factory: Callable[[], Awaitable[Any]]) -> Any:
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
        while len(_store) >= MAX_ENTRIES:
            del _store[next(iter(_store))]  # evict the oldest
    _store.pop(key, None)  # re-insert at the end
    _store[key] = (now + ttl, value)
    return value


def invalidate(prefix: str = "") -> None:
    for key in [k for k in _store if k.startswith(prefix)]:
        del _store[key]
