"""Derived-data background jobs: how a new feature gets the data it needs from
what is already stored, instead of re-reading Usenet.

A job is a versioned, idempotent, resumable step function. `step` handles one small
batch and returns how many rows it touched (0 = finished); it must pick up where it
left off by querying for "not done yet" rows, never by remembering a position. At
start-up every job whose recorded version is older than its code version runs to
completion in the background, with progress visible in the Status card. Bump a
job's `version` to make it run again over everything.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .cache import invalidate
from .models import JobState

logger = logging.getLogger("spotwebng")


@dataclass(frozen=True)
class Job:
    name: str
    version: int
    description: str
    step: Callable[[AsyncSession], Awaitable[int]]


REGISTRY: dict[str, Job] = {}
_running = False


def register(name: str, version: int, description: str):
    """Decorator: register `step(session) -> rows processed` as a job."""

    def wrap(step: Callable[[AsyncSession], Awaitable[int]]):
        REGISTRY[name] = Job(name, version, description, step)
        return step

    return wrap


async def _state(session: AsyncSession, name: str) -> JobState | None:
    return await session.get(JobState, name)


async def run_jobs(factory: async_sessionmaker[AsyncSession], pause: float = 0.05) -> None:
    """Run every job that is not up to date. One runner at a time."""
    global _running
    if _running:
        return
    _running = True
    try:
        for job in list(REGISTRY.values()):
            async with factory() as session:
                state = await _state(session, job.name)
                if state and state.status == "done" and state.version >= job.version:
                    continue
                if state is None or state.version != job.version:
                    state = JobState(name=job.name, version=job.version, processed=0)
                    await session.merge(state)
                state = await session.merge(state)
                state.status, state.detail = "running", ""
                state.updated_at = datetime.now(UTC)
                await session.commit()
            try:
                while True:
                    async with factory() as session:
                        touched = await job.step(session)
                        state = await _state(session, job.name)
                        state.processed += touched
                        state.updated_at = datetime.now(UTC)
                        if touched == 0:
                            state.status = "done"
                        await session.commit()
                    if touched == 0:
                        break
                    await asyncio.sleep(pause)  # let retrieval and requests breathe
            except Exception as exc:
                logger.exception("job %s failed", job.name)
                async with factory() as session:
                    state = await _state(session, job.name)
                    if state:
                        state.status, state.detail = "failed", type(exc).__name__
                        await session.commit()
    finally:
        _running = False
        invalidate()


async def rerun(factory: async_sessionmaker[AsyncSession], name: str) -> None:
    """Forget one job's progress and run it again (e.g. after an import brought in rows
    whose derived values must be recomputed locally)."""
    async with factory() as session:
        state = await _state(session, name)
        if state is not None:
            await session.delete(state)
            await session.commit()
    await run_jobs(factory)


async def job_status(session: AsyncSession) -> list[dict]:
    rows = {r.name: r for r in (await session.execute(select(JobState))).scalars().all()}
    out = []
    for job in REGISTRY.values():
        row = rows.get(job.name)
        stale = row is None or row.version < job.version  # never run, or run by older code
        out.append(
            {
                "name": job.name,
                "description": job.description,
                "status": "pending" if stale else row.status,
                "processed": row.processed if row else 0,
                "detail": row.detail if row else "",
            }
        )
    return out


async def reset_jobs(session: AsyncSession) -> int:
    """Forget all progress so every job runs again (the 'rebuild derived data' command)."""
    rows = (await session.execute(select(JobState))).scalars().all()
    for row in rows:
        await session.delete(row)
    await session.commit()
    return len(rows)
