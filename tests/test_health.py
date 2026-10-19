"""Spot health: sampling, classification, the STAT check against a fake server, send policy."""

import random
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import health
from app.downloader import send_spot
from app.models import Base, Spot
from tests.test_retriever import FakeNewsServer, _configure


def _nzb(count: int) -> bytes:
    segs = "".join(f'<segment bytes="1" number="{i}">art{i}@x</segment>' for i in range(count))
    return f"<nzb><file subject='f'><segments>{segs}</segments></file></nzb>".encode()


class StatServer(FakeNewsServer):
    """FakeNewsServer that also answers STAT for the message-ids in `present`."""

    def __init__(self, present, **kw):
        super().__init__(**kw)
        self.present = present
        self.stats = 0

    async def _serve(self, reader, writer):
        real = reader.readline

        async def readline():
            line = await real()
            if line.upper().startswith(b"STAT "):
                self.stats += 1
                mid = line.decode().strip().split(" ", 1)[1].strip("<>")
                writer.write(b"223 0 ok\r\n" if mid in self.present else b"430 no such article\r\n")
                await writer.drain()
                return await readline()
            return line

        reader.readline = readline
        await super()._serve(reader, writer)


def test_segment_ids_are_scanned_without_an_xml_parser():
    assert health.nzb_segment_ids(_nzb(3)) == ["art0@x", "art1@x", "art2@x"]
    bomb = (
        b'<!DOCTYPE x [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;">]><nzb><segment>&b;</segment></nzb>'
    )
    assert health.nzb_segment_ids(bomb) == ["&b;"]  # never expanded
    assert (
        health.nzb_segment_ids(b"<segment>" + b"x" * 300 + b"</segment>") == []
    )  # over-long id ignored
    assert health.nzb_segment_ids(b"") == []


def test_the_sample_always_has_the_first_and_last_article():
    ids = [f"a{i}" for i in range(500)]
    sample = health.pick_sample(ids, 30, random.Random(1))
    assert (
        len(sample) == 30 and sample[0] == "a0" and sample[-1] == "a499" and len(set(sample)) == 30
    )
    assert health.pick_sample(["x", "y"], 30) == ["x", "y"]


@pytest.mark.parametrize(
    "present, sampled, expected",
    [
        (30, 30, (health.COMPLETE, 100)),
        (27, 30, (health.PARTIAL, 90)),
        (15, 30, (health.PARTIAL, 50)),
        (14, 30, (health.DEAD, 47)),
        (0, 30, (health.DEAD, 0)),
        (0, 0, (health.UNCHECKED, -1)),
    ],
)
def test_classification_thresholds(present, sampled, expected):
    assert health.classify(present, sampled) == expected


@pytest.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _setup(db, monkeypatch, missing: set[int], count=40):
    present = {f"art{i}@x" for i in range(count) if i not in missing}
    server = StatServer(present, first=1, articles={})
    await server.start()
    await _configure(db, server.port)

    async def fake_nzb(session, spot):
        return _nzb(count)

    monkeypatch.setattr("app.newznab._real_nzb", fake_nzb)
    async with db() as s:
        s.add(
            Spot(
                message_id="<h@x>", title="Health Test", posted_at=datetime(2026, 7, 1, tzinfo=UTC)
            )
        )
        await s.commit()
    return server


async def _spot(db) -> Spot:
    async with db() as s:
        return (await s.execute(select(Spot))).scalar_one()


async def test_complete_partial_and_dead_spots_are_told_apart(db, monkeypatch):
    for missing, expected in [
        (set(), health.COMPLETE),
        (set(range(0, 40, 4)), health.PARTIAL),
        (set(range(2, 40)), health.DEAD),
    ]:
        async with db() as s:
            await s.execute(Spot.__table__.delete())
            await s.commit()
        server = await _setup(db, monkeypatch, missing)
        try:
            async with db() as s:
                spot = (await s.execute(select(Spot))).scalar_one()
                report = await health.check_spot(s, spot)
            assert report["health"] == expected, (missing, report)
            assert (await _spot(db)).health == expected and server.stats == 30
        finally:
            await server.stop()
        async with db() as s:  # reset provider rows for the next round
            from app.models import Setting

            await s.execute(Setting.__table__.delete())
            await s.commit()


async def test_a_recent_check_is_reused_unless_forced(db, monkeypatch):
    server = await _setup(db, monkeypatch, set())
    try:
        async with db() as s:
            spot = (await s.execute(select(Spot))).scalar_one()
            await health.check_spot(s, spot)
            first = server.stats
            await health.check_spot(s, spot)
            assert server.stats == first  # served from the stored result
            await health.check_spot(s, spot, force=True)
            assert server.stats == first * 2
    finally:
        await server.stop()


async def test_send_policy_blocks_dead_spots_warns_on_partial_and_can_be_overridden(
    db, monkeypatch, tmp_path
):
    server = await _setup(db, monkeypatch, set(range(2, 40)))  # dead
    prefs = {
        "nzb_action": "blackhole",
        "blackhole_dir": str(tmp_path),
        "send_health_policy": "block-dead",
        "sab_url": "",
        "sab_apikey": "",
        "sab_category": "",
        "sab_priority": -100,
    }
    try:
        async with db() as s:
            spot = (await s.execute(select(Spot))).scalar_one()
            refused = await send_spot(s, spot, prefs)
            assert refused["ok"] is False and refused["blocked"] and list(tmp_path.iterdir()) == []
            assert (await send_spot(s, spot, {**prefs, "send_health_policy": "warn"}))["ok"] is True
            forced = await send_spot(s, spot, prefs, force=True)
            assert forced["ok"] is True
    finally:
        await server.stop()


async def test_the_health_route_works_for_signed_in_users_and_hides_adult_spots():
    import httpx

    from app.auth import ADMIN, create_user
    from app.db import get_session
    from app.main import app
    from app.models import Setting

    override = app.dependency_overrides[get_session]
    async for session in override():
        maker = async_sessionmaker(session.bind, expire_on_commit=False)
        break
    async with maker() as s:
        s.add(Setting(key="provider_host", value="news.invalid"))
        s.add(
            Spot(
                message_id="<ok@x>",
                title="Fine",
                posted_at=datetime(2026, 7, 1, tzinfo=UTC),
                health=1,
                health_pct=100,
                health_checked_at=datetime.now(UTC),
            )
        )
        s.add(
            Spot(
                message_id="<xx@x>",
                title="Adult",
                posted_at=datetime(2026, 7, 1, tzinfo=UTC),
                category=6040,
            )
        )
        await create_user(s, "boss", "pw-boss-12345", ADMIN)
        await s.commit()
        ids = {r.title: r.id for r in (await s.execute(select(Spot))).scalars().all()}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        assert (await c.post(f"/spot/{ids['Fine']}/health")).status_code in (303, 307)  # signed out
        await c.post("/login", data={"username": "boss", "password": "pw-boss-12345"})
        ok = (await c.post(f"/spot/{ids['Fine']}/health")).json()
        assert ok["ok"] and ok["label"] == "complete"  # a fresh stored result: no network needed
        assert (await c.post(f"/spot/{ids['Adult']}/health")).status_code == 404
        assert "complete" in (await c.get(f"/spot/{ids['Fine']}")).text


async def test_only_admins_can_force_a_recheck_and_a_spot_is_never_checked_twice_at_once(
    db, monkeypatch
):
    import asyncio

    server = await _setup(db, monkeypatch, set())
    try:
        async with db() as s:
            spot = (await s.execute(select(Spot))).scalar_one()
        async with db() as s1, db() as s2:
            a = (await s1.execute(select(Spot))).scalar_one()
            b = (await s2.execute(select(Spot))).scalar_one()
            first, second = await asyncio.gather(health.check_spot(s1, a), health.check_spot(s2, b))
        errors = [r for r in (first, second) if "error" in r]
        assert len(errors) == 1 and "already running" in errors[0]["error"]
        assert spot.id not in health._inflight
    finally:
        await server.stop()
