"""Retrieval progress: what is running now (in memory) and what we last learned
about the news server (persisted, so it survives a restart).

`LIVE` is per process; the app runs as one uvicorn worker. The persisted status
is a single JSON row in `settings` so it needs no schema change.
"""

from __future__ import annotations

import json
import time
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from .models import Setting

KEY = "retrieval_status"

LIVE: dict[str, Any] = {
    "running": False,
    "phase": "idle",
    "detail": "",
    "percent": None,
    "started": None,
    "updated": None,
}


def live_set(**fields: Any) -> None:
    LIVE.update(fields, updated=time.time())


def live_idle() -> None:
    live_set(running=False, phase="idle", detail="", percent=None)


async def load_status(session: AsyncSession) -> dict[str, Any]:
    row = await session.get(Setting, KEY)
    if row is None or not row.value:
        return {}
    try:
        data = json.loads(row.value)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


async def save_status(session: AsyncSession, **fields: Any) -> None:
    """Merge `fields` into the persisted status. The caller commits."""
    data = await load_status(session)
    data.update(fields)
    row = await session.get(Setting, KEY)
    if row is None:
        session.add(Setting(key=KEY, value=json.dumps(data)))
    else:
        row.value = json.dumps(data)


def history_percent(status: dict[str, Any]) -> float | None:
    """Share of the server's article range already scanned (newest -> oldest)."""
    if status.get("days_reached_for"):
        return 100.0  # the day limit ended the walk
    first, last, floor = status.get("group_first"), status.get("group_last"), status.get("floor")
    if not all(isinstance(v, int) for v in (first, last, floor)) or last <= first:
        return None
    return round(max(0.0, min(100.0, (last - floor) / (last - first) * 100)), 1)
