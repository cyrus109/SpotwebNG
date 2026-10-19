"""Derived columns and the FTS5 search index: built by jobs, kept in sync, safe fallback."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import cache, derived, jobs, migrate  # noqa: F401  (derived registers the jobs)
from app.models import Spot
from app.spots import adult_condition, fts_match, search_spots

WHEN = datetime(2026, 7, 1, tzinfo=UTC)


@pytest.fixture
async def env(tmp_path, monkeypatch):
    monkeypatch.setattr(migrate, "SPOT_DATA_DIR", tmp_path / "data")
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/t.db")
    await migrate.upgrade_database(engine)
    cache.invalidate()
    yield async_sessionmaker(engine, expire_on_commit=False)
    cache.invalidate()
    await engine.dispose()


async def _add(factory, *titles, **kw):
    async with factory() as s:
        for i, title in enumerate(titles):
            s.add(Spot(message_id=f"<m{i}-{title[:5]}@x>", title=title, posted_at=WHEN, **kw))
        await s.commit()


def test_match_expressions():
    assert fts_match("Silo S03E05") == '"Silo"* AND "S03E05"*'
    assert fts_match("mr. robot") == '"mr"* AND "robot"*'
    assert fts_match("a b") is None  # too short to index: LIKE is used instead
    assert fts_match('"; DROP TABLE spots; --') == '"DROP"* AND "TABLE"* AND "spots"*'
    assert fts_match("!!!") is None


async def test_search_uses_like_until_the_index_is_built_then_fts(env):
    await _add(env, "Silo.S03E05.Memory.1080p", "Severance S02E01 720p")
    async with env() as s:
        rows, _ = await search_spots(s, query="silo s03e05")
    assert [r.title for r in rows] == ["Silo.S03E05.Memory.1080p"]  # LIKE path works before the job

    await jobs.run_jobs(env, pause=0)
    cache.invalidate()
    async with env() as s:
        assert (
            await s.execute(text("SELECT count(*) FROM spots_fts WHERE spots_fts MATCH 'silo'"))
        ).scalar_one() == 1
        rows, total = await search_spots(s, query="silo s03e05")
        assert [r.title for r in rows] == ["Silo.S03E05.Memory.1080p"] and total == 1
        assert (await search_spots(s, query="sev"))[1] == 1  # word prefix
        assert (await search_spots(s, query="ilo"))[1] == 0  # not a substring search (documented)


async def test_the_index_follows_inserts_updates_and_deletes(env):
    await jobs.run_jobs(env, pause=0)
    cache.invalidate()
    await _add(env, "Alpha Movie 2020")
    async with env() as s:
        assert (await search_spots(s, query="alpha"))[1] == 1  # insert trigger
        spot = (await s.execute(select(Spot))).scalar_one()
        spot.title = "Bravo Movie 2020"
        await s.commit()
        assert (await search_spots(s, query="alpha"))[1] == 0 and (
            await search_spots(s, query="bravo")
        )[1] == 1
        await s.delete(spot)
        await s.commit()
        assert (await search_spots(s, query="bravo"))[1] == 0
        assert (
            await s.execute(text("INSERT INTO spots_fts(spots_fts) VALUES('integrity-check')"))
        ) is not None


async def test_a_rebuild_survives_rows_that_changed_before_the_index_existed(env):
    await _add(env, "Old Row One")  # indexed by the insert trigger only
    async with (
        env() as s
    ):  # edited and deleted before the index is "ready": no update/delete triggers yet
        spot = (await s.execute(select(Spot))).scalar_one()
        spot.title = "Renamed Row"
        await s.commit()
    await jobs.run_jobs(env, pause=0)
    cache.invalidate()
    async with env() as s:
        assert (await search_spots(s, query="renamed"))[1] == 1
        assert (await search_spots(s, query="old"))[1] == 0
        await s.execute(text("INSERT INTO spots_fts(spots_fts) VALUES('integrity-check')"))


async def test_adult_flag_and_title_keys_are_derived_and_never_leak_in_between(env):
    async with env() as s:  # as if stored before the columns existed: adult = -1, title_key = ''
        s.add(Spot(message_id="<a@x>", title="Dune 2024 2160p", posted_at=WHEN, category=2040))
        s.add(Spot(message_id="<b@x>", title="Spicy", posted_at=WHEN, category=6040))
        s.add(
            Spot(
                message_id="<c@x>",
                title="Spicy in Movies",
                posted_at=WHEN,
                category=2040,
                subcat_z="z3|",
            )
        )
        await s.commit()
        hidden = (await s.execute(select(Spot.title).where(adult_condition()))).scalars().all()
        assert sorted(hidden) == ["Spicy", "Spicy in Movies"]  # fallback works before the job
    await jobs.run_jobs(env, pause=0)
    async with env() as s:
        rows = {r.title: r for r in (await s.execute(select(Spot))).scalars().all()}
        assert [rows[t].adult for t in ("Dune 2024 2160p", "Spicy", "Spicy in Movies")] == [0, 1, 1]
        assert rows["Dune 2024 2160p"].title_key == "dune 2024"
        hidden = (await s.execute(select(Spot.title).where(adult_condition()))).scalars().all()
        assert sorted(hidden) == ["Spicy", "Spicy in Movies"]


# ---- caching and keyset paging ----


async def test_counts_are_cached_for_a_minute_and_invalidated_explicitly(env, monkeypatch):
    monkeypatch.setattr(cache, "enabled", True)
    cache.invalidate()
    await _add(env, "One")
    async with env() as s:
        assert (await search_spots(s))[1] == 1
    await _add(env, "Two Three")
    async with env() as s:
        assert (await search_spots(s))[1] == 1  # cached: the total lags by design
        cache.invalidate()
        assert (await search_spots(s))[1] == 2  # ...until a retrieval/import/block invalidates it


@pytest.mark.parametrize("sort", ["new", "old"])
async def test_keyset_paging_visits_every_row_once_even_with_equal_timestamps(env, sort):
    async with env() as s:
        for i in range(25):  # five rows share each timestamp
            s.add(
                Spot(
                    message_id=f"<k{i}@x>",
                    title=f"T{i}",
                    posted_at=datetime(2026, 7, 1 + i // 5, tzinfo=UTC),
                )
            )
        await s.commit()
    seen, after = [], None
    for _ in range(10):
        async with env() as s:
            rows, total = await search_spots(s, sort=sort, limit=4, after=after)
        assert total == 25
        if not rows:
            break
        seen += [r.id for r in rows]
        after = (rows[-1].posted_at.replace(tzinfo=UTC), rows[-1].id)
    assert len(seen) == 25 == len(set(seen))
    async with env() as s:
        offset_order, _ = await search_spots(s, sort=sort, limit=200)
    assert seen == [r.id for r in offset_order]  # same order as the OFFSET pages


async def test_backfilling_subcategories_recomputes_the_adult_flag(env):
    """A row stored before subcats existed gets its z3 later; the flag must follow."""
    from app.retriever import _store_rows

    async with env() as s:
        s.add(
            Spot(message_id="<s@x>", title="Spicy", posted_at=WHEN, category=2040, adult=0)
        )  # flagged safe
        await s.commit()
    row = {
        "Number": 1,
        "Subject": "Spicy | tag",
        "From": "Nick <k@1701.1000.r.1700000000.0.0.sig>",  # key id 7 -> hashcash decides below
        "Date": "Fri, 24 Jul 2026 10:00:00 +0200",
        "Message-ID": "<s@x>",
    }
    # build a parseable spot whose subcat string carries z3
    row["From"] = "Nick <k@1701z03.1000.r.1700000000.0.0.sig>"
    async with env() as s:
        import hashlib

        n = 0
        while True:  # a message-id that passes the key-id-7 hashcash
            mid = f"s{n}@x"
            if hashlib.sha1(f"<{mid}>".encode()).hexdigest().startswith("0000"):
                break
            n += 1
        async with env() as s2:
            spot = (await s2.execute(select(Spot))).scalar_one()
            spot.message_id = f"<{mid}>"
            await s2.commit()
        row["Message-ID"] = f"<{mid}>"
        await _store_rows(s, [row])
        await s.commit()
        refreshed = (await s.execute(select(Spot))).scalar_one()
    assert "z3|" in refreshed.subcat_z and refreshed.adult == 1


async def test_the_cache_is_bounded_whatever_users_search_for(monkeypatch):
    monkeypatch.setattr(cache, "enabled", True)
    monkeypatch.setattr(cache, "MAX_ENTRIES", 50)
    cache.invalidate()

    async def make():
        return 1

    for i in range(500):  # 500 distinct "searches"
        await cache.cached(f"total:q{i}", 60, make)
    assert len(cache._store) <= 50
    assert "total:q499" in cache._store and "total:q0" not in cache._store  # newest kept
    await cache.cached("total:" + "x" * 5000, 60, make)  # oversized key: computed, never stored
    assert not any(len(k) > cache.MAX_KEY_LENGTH for k in cache._store)
    cache.invalidate()
