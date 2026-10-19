"""Storage figures for the Status card: export-size estimate, disk usage, low-space warning."""

import random
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import autobackup, storage
from app.backup import estimate_export_bytes, export_chunks
from app.models import Base, Comment, Spot


@pytest.fixture
async def factory(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/t.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _fill(factory, spots=3000, comments=500):
    rng = random.Random(7)
    words = "dune silo bear oppenheimer severance matrix alien heat fargo".split()
    base = datetime(2026, 1, 1, tzinfo=UTC)
    async with factory() as s:
        for i in range(spots):
            title = f"{rng.choice(words).title()} {rng.choice(words).title()} {2000 + i % 25} {rng.choice(['1080p', '2160p', '720p'])} WEB-DL x265-GRP{i % 40}"
            s.add(
                Spot(
                    message_id=f"<s{i}.{rng.randrange(10**9)}@spot.example>",
                    title=title,
                    poster=f"Poster{i % 90}",
                    category=2000 + (i % 5) * 10,
                    size=rng.randrange(10**8, 10**10),
                    posted_at=base + timedelta(minutes=i),
                    subcat_a="a9|",
                    subcat_z="z0|",
                    description="A fairly ordinary description of the release. " * 3,
                    from_hdr=f"Poster{i % 90} <{'k' * 60}.{'s' * 60}@17{i}.1000.{rng.randrange(10**9)}.17000000{i % 99}.0.0.{'x' * 64}>",
                )
            )
        for i in range(comments):
            s.add(
                Comment(
                    message_id=f"<c{i}.1.a.b@x>",
                    spot_message_id=f"<s{i}@x>",
                    poster="Al",
                    posted_at=base,
                    body="nice " * 20,
                    body_state=1,
                )
            )
        await s.commit()


async def test_the_export_estimate_is_close_to_the_real_size(factory):
    await _fill(factory)
    actual = sum([len(chunk) async for chunk in export_chunks(factory)])
    async with factory() as s:
        estimate = await estimate_export_bytes(s)
    assert 0.6 * actual <= estimate <= 1.6 * actual, (estimate, actual)


async def test_an_empty_database_estimates_to_nothing(factory):
    async with factory() as s:
        assert await estimate_export_bytes(s) == 0


async def test_collect_reports_the_data_folder_and_the_disk(factory, tmp_path, monkeypatch):
    monkeypatch.setattr("app.storage.config.SPOT_DATA_DIR", tmp_path / "data")
    monkeypatch.setattr("app.autobackup.config.SPOT_DATA_DIR", tmp_path / "data")
    images, nzbs = tmp_path / "data" / "images", tmp_path / "data" / "nzbs"
    monkeypatch.setattr("app.storage.config.IMAGES_DIR", images)
    monkeypatch.setattr("app.storage.config.NZB_DIR", nzbs)
    (images / "sub").mkdir(parents=True)
    nzbs.mkdir(parents=True)
    (images / "1.jpg").write_bytes(b"x" * 1000)
    (images / "sub" / "2.jpg").write_bytes(b"x" * 500)
    (nzbs / "1.nzb").write_bytes(b"y" * 200)
    await _fill(factory, spots=50, comments=5)
    made = await autobackup.make_backup(factory)
    async with factory() as s:
        info = await storage.collect(s)
    assert (info["covers_bytes"], info["nzb_bytes"]) == (1500, 200)
    assert (
        info["database_bytes"] > 0
        and info["free_bytes"] > 0
        and info["total_bytes"] >= info["free_bytes"]
    )
    assert info["backups"]["count"] == 1 and info["backups"]["latest_bytes"] == made.stat().st_size
    assert info["export_estimate_bytes"] > 0


def test_the_warning_appears_when_the_disk_cannot_hold_the_planned_backups():
    gb = 10**9
    roomy = {"export_estimate_bytes": 1 * gb // 100, "free_bytes": 100 * gb}
    assert storage.warning(roomy, keep=3) is None
    tight = {
        "export_estimate_bytes": 20 * gb,
        "free_bytes": 50 * gb,
    }  # 3 x 20 GB, with headroom, does not fit
    message = storage.warning(tight, keep=3)
    assert (
        message
        and "Low disk space" in message
        and "20,000 MB" in message
        and "50.0 GB free" in message
    )
    assert storage.warning(tight, keep=1) is None  # one backup fits
    assert (
        storage.warning({"export_estimate_bytes": 0, "free_bytes": 0}, keep=0) is not None
    )  # a full disk is a problem
