"""SQLite tuning for a big index on a slow disk: pragmas, write-ahead-log size, checkpoints,
the enrichment query plan, and the snapshot-holding bugs that let the log grow to 8 GB."""

import shutil
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import db, migrate
from app.main import backoff_delay
from app.models import Base, Spot


@pytest.fixture
async def engine(tmp_path, monkeypatch):
    # a short busy timeout, so "blocked by a reader" shows up as busy=1 instead of a 30 s wait
    pragmas = tuple(
        "PRAGMA busy_timeout=300" if p.startswith("PRAGMA busy_timeout") else p
        for p in db.SQLITE_PRAGMAS
    )
    monkeypatch.setattr(db, "SQLITE_PRAGMAS", pragmas)
    eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/t.db")
    db.install_sqlite_pragmas(eng)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


async def _pragma(engine, name):
    async with engine.connect() as conn:
        return (await conn.exec_driver_sql(f"PRAGMA {name}")).scalar()


async def test_every_connection_gets_the_big_database_settings(engine):
    assert str(await _pragma(engine, "journal_mode")).lower() == "wal"
    assert (
        await _pragma(engine, "journal_size_limit") == 134217728
    )  # the log is cut back to 128 MiB
    assert await _pragma(engine, "cache_size") == -16384  # 16 MiB instead of the 2 MiB default
    assert await _pragma(engine, "temp_store") == 2  # MEMORY
    assert await _pragma(engine, "mmap_size") == 268435456
    assert await _pragma(engine, "synchronous") == 1  # NORMAL


def _fill(n, start=0, text_len=200):
    when = datetime(2026, 7, 1, tzinfo=UTC)
    return [
        Spot(
            message_id=f"<w{start + i}@x>",
            title=f"Title {start + i}",
            posted_at=when,
            description="d" * text_len,
        )
        for i in range(n)
    ]


async def test_a_long_running_read_pins_the_log_until_it_finishes(engine, tmp_path):
    """Checkpoint starvation: while any read statement is still running, SQLite cannot restart or
    shrink its write-ahead log. The old enrichment query (a sort over a million rows, every 20 s
    on a slow disk) meant a read was always running, so the log could only grow."""
    import sqlite3

    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        s.add_all(_fill(50, 0))
        await s.commit()
    reader = sqlite3.connect(tmp_path / "t.db")
    cursor = reader.execute("SELECT * FROM spots")
    cursor.fetchone()  # a statement that is still running: it holds a read snapshot
    async with maker() as writer:
        for batch in range(8):  # many small commits, like per-spot enrichment used to do
            writer.add_all(_fill(200, 1000 + batch * 200, 400))
            await writer.commit()
    assert db.wal_bytes(engine) > 200_000
    busy, _log, _done = await db.checkpoint(engine, "TRUNCATE")
    assert busy == 1 and db.wal_bytes(engine) > 0  # blocked by the running read
    cursor.close()  # the long statement ends
    busy, _log, _done = await db.checkpoint(engine, "TRUNCATE")
    assert busy == 0 and db.wal_bytes(engine) == 0  # emptied and truncated
    reader.close()


async def test_tame_wal_only_acts_above_its_thresholds(engine):
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        for batch in range(6):
            s.add_all(_fill(200, batch * 200, 400))
            await s.commit()
    big = db.wal_bytes(engine)
    assert big > 100_000
    assert await db.tame_wal(engine, soft=10**9, hard=10**10) == big  # below the limits: untouched
    assert await db.tame_wal(engine, soft=1, hard=10) == 0  # above them: checkpointed and truncated


async def test_checkpoints_are_a_noop_on_other_databases_and_reject_bad_modes():
    class Url:
        def get_backend_name(self):
            return "postgresql"

    class Pg:
        url = Url()

    assert await db.checkpoint(Pg(), "PASSIVE") is None
    assert db.wal_bytes(Pg()) == 0
    with pytest.raises(ValueError):
        await db.checkpoint(None, "TRUNCATE; DROP TABLE spots")


async def test_enrichment_commits_once_per_batch_not_once_per_spot(tmp_path):
    """Every commit rewrites the touched pages of ~12 indexes and the search index into the log."""
    from sqlalchemy import event

    from app.models import Setting
    from app.retriever import enrich_new_spots
    from tests.test_retriever import FakeNewsServer, _configure

    server = FakeNewsServer(first=1, articles={})
    await server.start()
    eng = create_async_engine("sqlite+aiosqlite://")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(eng, expire_on_commit=False)
    await _configure(maker, server.port)
    async with maker() as s:
        s.add(Setting(key="connections", value="4"))
        s.add_all(_fill(6, 0))
        await s.commit()
    commits = []
    try:
        async with maker() as s:
            event.listen(s.sync_session, "after_commit", lambda _s: commits.append(1))
            result = await enrich_new_spots(s, images_dir=tmp_path / "img", limit=10)
        assert result["failed"] == 6  # the fake server has none of them: each is marked failed
        assert len(commits) == 2  # 6 spots over 4 connections = two batches, two commits (was six)
    finally:
        await server.stop()
        await eng.dispose()


def test_enrichment_backs_off_while_the_news_server_is_failing():
    delays = [backoff_delay(n) for n in range(0, 8)]
    assert delays[0] == 20 and delays[1] == 40 and delays[2] == 80
    assert delays == sorted(delays) and delays[-1] == 600  # grows, then stops at ten minutes
    assert backoff_delay(-3) == 20


async def test_the_enrichment_query_is_served_from_one_index_with_no_sort(tmp_path, monkeypatch):
    monkeypatch.setattr(migrate, "SPOT_DATA_DIR", tmp_path / "data")
    eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/m.db")
    try:
        await migrate.upgrade_database(eng)
        async with eng.connect() as conn:
            for sql in (
                "SELECT * FROM spots WHERE enriched = 0 ORDER BY posted_at DESC LIMIT 50",
                "SELECT * FROM spots WHERE enriched = 0 AND posted_at >= '2026-09-01' ORDER BY posted_at DESC LIMIT 50",
            ):
                plan = " | ".join(
                    r[3] for r in await conn.exec_driver_sql("EXPLAIN QUERY PLAN " + sql)
                )
                assert "ix_spots_enriched_posted_at" in plan, plan
                assert "TEMP B-TREE" not in plan, plan  # SQLite no longer sorts a million rows
            names = {r[1] for r in await conn.exec_driver_sql("PRAGMA index_list(spots)")}
        assert "ix_spots_enriched" not in names  # the redundant single-column index is gone
    finally:
        await eng.dispose()


async def test_the_new_index_upgrades_an_existing_database_in_place(tmp_path, monkeypatch):
    monkeypatch.setattr(migrate, "SPOT_DATA_DIR", tmp_path / "data")
    eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/u.db")
    try:
        await migrate.upgrade_database(eng, target="0007")
        async with eng.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO spots (message_id, title, poster, category, size, posted_at, subcat_a, subcat_b,"
                    " subcat_c, subcat_d, subcat_z, spot_category, imdb_id, tvmaze_id, poster_url, description,"
                    " nzb_ref, enriched, website, nzb_segments, keyid, spotter_id, verified, from_hdr, adult,"
                    " title_key, health, health_pct) VALUES ('<a@x>', 'Kept', '', 0, 0, '2026-01-01', '', '', '',"
                    " '', '', -1, '', '', '', '', '', 0, '', '', -1, '', 0, '', -1, '', 0, -1)"
                )
            )
        result = await migrate.upgrade_database(eng)
        assert result["from"] == "0007" and result["to"] == migrate.head_revision()
        async with eng.connect() as conn:
            assert (await conn.execute(text("SELECT title FROM spots"))).scalar_one() == "Kept"
            names = {r[1] for r in await conn.exec_driver_sql("PRAGMA index_list(spots)")}
        assert "ix_spots_enriched_posted_at" in names and "ix_spots_enriched" not in names
    finally:
        await eng.dispose()


def test_safety_copies_are_limited_and_skipped_when_the_disk_is_too_full(tmp_path, monkeypatch):
    monkeypatch.setattr(migrate, "SPOT_DATA_DIR", tmp_path)
    source = tmp_path / "live.db"
    import sqlite3

    con = sqlite3.connect(source)
    con.execute("CREATE TABLE t (x)")
    con.commit()
    con.close()
    folder = tmp_path / "backups"
    folder.mkdir()
    for i in range(4):
        (folder / f"pre-migrate-0001-to-0002-2026010{i}-000000.db").write_bytes(b"x")
    made = migrate.backup_sqlite(str(source), "0007", "0008")
    assert made is not None
    assert len(list(folder.glob("pre-migrate-*.db"))) == migrate.KEEP_PRE_MIGRATION_BACKUPS == 2

    class Full:
        free = 0

    monkeypatch.setattr(shutil, "disk_usage", lambda _p: Full())
    assert migrate.backup_sqlite(str(source), "0008", "0009") is None  # refuses to fill the disk
    assert len(list(folder.glob("pre-migrate-*.db"))) == 2
