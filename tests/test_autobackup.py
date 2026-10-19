"""Automatic backups: written, pruned, due-checked, and restorable."""

import os
import time
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import autobackup
from app.backup import import_file
from app.models import Base, Setting, Spot


@pytest.fixture
async def factory(tmp_path, monkeypatch):
    monkeypatch.setattr("app.autobackup.config.SPOT_DATA_DIR", tmp_path)
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        for i in range(3):
            s.add(
                Spot(
                    message_id=f"<b{i}@x>",
                    title=f"T{i}",
                    posted_at=datetime(2026, 1, 1, tzinfo=UTC),
                )
            )
        await s.commit()
    yield maker
    await engine.dispose()


async def test_a_backup_restores_into_a_fresh_install(factory, tmp_path):
    made = await autobackup.make_backup(factory)
    assert made.parent == tmp_path / "backups" and not list((tmp_path / "backups").glob("*.part"))
    fresh = create_async_engine("sqlite+aiosqlite://")
    async with fresh.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    target = async_sessionmaker(fresh, expire_on_commit=False)
    try:
        await import_file(target, str(made))
        async with target() as s:
            assert (await s.execute(select(func.count()).select_from(Spot))).scalar_one() == 3
    finally:
        await fresh.dispose()


async def test_prune_keeps_only_the_newest(factory, tmp_path):
    folder = tmp_path / "backups"
    folder.mkdir()
    for i in range(5):
        path = folder / f"spotwebng-2026010{i}-000000.jsonl.gz"
        path.write_bytes(b"x")
    assert autobackup.prune(2) == 3
    assert [p.name[-24:] for p in autobackup.list_backups()] == [
        "20260103-000000.jsonl.gz",
        "20260104-000000.jsonl.gz",
    ]
    assert autobackup.summary()["count"] == 2


async def test_backups_run_only_when_enabled_and_due(factory, tmp_path):
    async with factory() as s:
        s.add(Setting(key="backup_keep", value="0"))
        await s.commit()
    assert await autobackup.backup_if_due(factory) is None  # off
    async with factory() as s:
        (await s.get(Setting, "backup_keep")).value = "2"
        await s.commit()
    first = await autobackup.backup_if_due(factory)
    assert first is not None
    assert await autobackup.backup_if_due(factory) is None  # fresh enough
    old = time.time() - 30 * 3600
    os.utime(first, (old, old))
    time.sleep(1.1)  # distinct timestamp in the new file's name
    assert await autobackup.backup_if_due(factory) is not None
    assert autobackup.summary()["count"] == 2
