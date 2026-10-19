"""The derived-data job runner: batches, resume, version bumps, failures."""

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import jobs
from app.models import Base


@pytest.fixture
async def factory():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture(autouse=True)
def clean_registry():
    saved = dict(jobs.REGISTRY)
    jobs.REGISTRY.clear()
    yield
    jobs.REGISTRY.clear()
    jobs.REGISTRY.update(saved)


async def _status(factory):
    async with factory() as s:
        return {j["name"]: j for j in await jobs.job_status(s)}


async def test_a_job_runs_in_batches_until_it_reports_zero(factory):
    todo = [3, 3, 1, 0]
    calls = []

    @jobs.register("count", 1, "Counting")
    async def step(session):
        calls.append(1)
        return todo[len(calls) - 1]

    await jobs.run_jobs(factory, pause=0)
    status = (await _status(factory))["count"]
    assert status["status"] == "done" and status["processed"] == 7 and len(calls) == 4

    await jobs.run_jobs(factory, pause=0)  # up to date: not run again
    assert len(calls) == 4


async def test_bumping_the_version_reruns_the_job(factory):
    seen = []

    @jobs.register("flag", 1, "Flagging")
    async def step(session):
        seen.append("run")
        return 0

    await jobs.run_jobs(factory, pause=0)
    jobs.REGISTRY["flag"] = jobs.Job("flag", 2, "Flagging", step)
    assert (await _status(factory))["flag"]["status"] == "pending"
    await jobs.run_jobs(factory, pause=0)
    assert seen == ["run", "run"] and (await _status(factory))["flag"]["status"] == "done"


async def test_a_failing_job_is_recorded_and_does_not_stop_the_others(factory):
    @jobs.register("bad", 1, "Broken")
    async def bad(session):
        raise RuntimeError("boom")

    ran = []

    @jobs.register("good", 1, "Fine")
    async def good(session):
        ran.append(1)
        return 0

    await jobs.run_jobs(factory, pause=0)
    status = await _status(factory)
    assert status["bad"]["status"] == "failed" and status["bad"]["detail"] == "RuntimeError"
    assert status["good"]["status"] == "done" and ran == [1]
    async with factory() as s:
        assert await jobs.reset_jobs(s) == 2
    assert (await _status(factory))["bad"]["status"] == "pending"
