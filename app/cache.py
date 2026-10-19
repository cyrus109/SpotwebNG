"""A tiny in-process TTL cache for expensive read-only aggregates (counts, stats).

One process, one worker: no locking or eviction beyond expiry. `invalidate()` is
called after a retrieval commits new rows so cached counts don't lag behind it.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

_store: dict[str, tuple[float, Any]] = {}


async def cached(key: str, ttl: float, factory: Callable[[], Awaitable[Any]]) -> Any:
    now = time.monotonic()
    hit = _store.get(key)
    if hit and hit[0] > now:
        return hit[1]
    value = await factory()
    _store[key] = (now + ttl, value)
    return value


def invalidate(prefix: str = "") -> None:
    for key in [k for k in _store if k.startswith(prefix)]:
        del _store[key]
