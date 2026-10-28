"""Facet counts are built in the background and only replaced by a refresh."""

import asyncio

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import cache, config, facets
from app.models import Base
from tests.test_facets import _spot


@pytest.fixture
async def env(tmp_path, monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(facets, "SessionLocal", factory)
    monkeypatch.setattr(config, "SPOT_DATA_DIR", tmp_path)
    monkeypatch.setattr(facets, "SPOT_DATA_DIR", tmp_path)
    monkeypatch.setattr(cache, "enabled", True)
    monkeypatch.setattr(facets, "_known", {})
    monkeypatch.setattr(facets, "updated_at", None)
    cache.invalidate(cache.STICKY)
    async with factory() as s:
        s.add_all([_spot(i) for i in range(3)])
        await s.commit()
    yield factory
    cache.invalidate(cache.STICKY)
    await engine.dispose()


async def _count(factory) -> int | None:
    async with factory() as s:
        return (await facets.section_counts(s)).get("all")


async def test_page_gets_a_count_once_then_keeps_it(env):
    assert await _count(env) == 3
    async with env() as s:
        s.add(_spot(99))
        await s.commit()
    assert await _count(env) == 3  # no timer: the old number stands until a refresh


async def test_refresh_replaces_counts_and_writes_a_snapshot(env, tmp_path):
    assert await _count(env) == 3
    async with env() as s:
        s.add(_spot(99))
        await s.commit()
    await facets.refresh_facets()
    assert await _count(env) == 4
    assert (tmp_path / facets.SNAPSHOT_NAME).exists()
    assert facets.updated_at is not None


async def test_snapshot_survives_a_restart(env, monkeypatch):
    assert await _count(env) == 3
    await facets.refresh_facets()
    cache.invalidate(cache.STICKY)
    monkeypatch.setattr(facets, "_known", {})
    assert facets.load_snapshot()
    assert cache.peek(facets._key("sections", (), {}))["all"] == 3


async def test_missing_facet_does_not_block_the_page(env, monkeypatch):
    monkeypatch.setattr(facets, "WAIT", 0.05)
    gate = asyncio.Event()
    fn, default = facets._RAW["sections"]

    async def slow(session):
        await gate.wait()
        return await fn(session)

    monkeypatch.setitem(facets._RAW, "sections", (slow, default))
    assert await _count(env) is None  # rendered without the numbers
    gate.set()
    await asyncio.sleep(0.3)  # the background build lands
    assert await _count(env) == 3


async def test_mark_stale_coalesces_into_one_refresh(env, monkeypatch):
    runs = []

    async def fake():
        runs.append(1)
        await asyncio.sleep(0.05)

    monkeypatch.setattr(facets, "refresh_facets", fake)
    monkeypatch.setattr(facets, "REFRESH_DELAY", 0.01)
    for _ in range(5):
        facets.mark_stale()
    await asyncio.sleep(0.4)
    assert 1 <= len(runs) <= 2  # one run, plus at most one repeat for changes during it


def test_invalidate_keeps_facets_unless_asked(monkeypatch):
    monkeypatch.setattr(cache, "enabled", True)
    cache.store("facet:x", 1)
    cache.store("total:x", 2)
    cache.invalidate()
    assert cache.peek("facet:x") == 1
    assert cache.peek("total:x") is cache.MISSING
    cache.invalidate("facet:")
    assert cache.peek("facet:x") is cache.MISSING


def test_eviction_never_drops_facets(monkeypatch):
    monkeypatch.setattr(cache, "enabled", True)
    monkeypatch.setattr(cache, "MAX_ENTRIES", 5)
    cache.invalidate("facet:")
    cache.store("facet:keep", 1)

    async def fill():
        for i in range(12):
            await cache.cached(f"total:{i}", 60, lambda i=i: asyncio.sleep(0, i))

    asyncio.run(fill())
    assert cache.peek("facet:keep") == 1
    cache.invalidate("facet:")


async def test_refresh_keeps_the_old_value_when_one_key_fails(env, monkeypatch):
    assert await _count(env) == 3
    _fn, default = facets._RAW["sections"]

    async def boom(session):
        raise RuntimeError("database is locked")

    monkeypatch.setitem(facets._RAW, "sections", (boom, default))
    await facets.refresh_facets()
    assert await _count(env) == 3  # old number kept, refresh did not abort


async def test_pending_builds_and_sticky_entries_are_capped(env, monkeypatch):
    monkeypatch.setattr(facets, "WAIT", 0.01)
    monkeypatch.setattr(facets, "MAX_PENDING", 2)
    gate = asyncio.Event()
    fn, default = facets._RAW["options"]

    async def slow(session, tree):
        await gate.wait()
        return []

    monkeypatch.setitem(facets._RAW, "options", (slow, default))
    for n in range(6):
        async with env() as s:
            await facets.option_counts(s, f"cat0_z{n}")
    assert len(facets._pending) == 2
    gate.set()
    await asyncio.sleep(0.1)
    monkeypatch.setattr(cache, "MAX_STICKY", 3)
    for n in range(10):
        cache.store(f"facet:t{n}", n)
    assert len([k for k in cache._store if k.startswith("facet:")]) <= 3
