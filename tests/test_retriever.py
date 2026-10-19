"""Integration tests for the NNTP spot retriever against a fake news server."""

import asyncio
import json
import zlib
from datetime import UTC

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import Base, Setting, Spot, UsenetState
from app.retriever import retrieve_spots
from tests.test_spotnet import SAMPLE_XML

GOOD_USER, GOOD_PASS = "alice", "s3cret"

SPOT_FROM = "Nick{n} <k@14a09b03c02d05.73400320{n}.r.20260101.n.n.SIG>"


def _xover_line(n: int) -> str:
    return "\t".join(
        [
            str(n),
            f"Spot Title {n} | grp",
            SPOT_FROM.format(n=n),
            "Fri, 24 Jul 2026 10:00:00 +0200",
            f"<spot-{n}@spot.example>",
            "",
            "1000",
            "10",
        ]
    )


class FakeNewsServer:
    """Speaks just enough NNTP: auth, GROUP, XOVER, XHDR Message-ID."""

    def __init__(
        self, first: int = 100, articles: dict[int, str] | None = None, max_clients: int = 99
    ):
        self.max_clients = max_clients
        self.connections = 0  # total ever accepted
        self.concurrent = 0  # open right now
        self.peak = 0
        self.first = first
        self.articles = articles if articles is not None else {}
        self.server = None
        self.port = None

    @property
    def last(self) -> int:
        return max(self.articles, default=self.first)

    async def start(self):
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self):
        self.server.close()
        await self.server.wait_closed()

    async def _handle(self, reader, writer):
        self.connections += 1
        if self.concurrent >= self.max_clients:
            writer.write(b"502 too many connections\r\n")
            await writer.drain()
            writer.close()
            return
        self.concurrent += 1
        self.peak = max(self.peak, self.concurrent)
        try:
            await self._serve(reader, writer)
        finally:
            self.concurrent -= 1

    async def _serve(self, reader, writer):
        writer.write(b"200 fake ready\r\n")
        await writer.drain()
        while True:
            line = await reader.readline()
            if not line:
                break
            cmd = line.decode().strip()
            upper = cmd.upper()
            if upper.startswith("AUTHINFO USER"):
                writer.write(b"381 pw\r\n")
            elif upper.startswith("AUTHINFO PASS"):
                writer.write(b"281 ok\r\n")
            elif upper.startswith("GROUP"):
                writer.write(
                    f"211 {len(self.articles)} {self.first} {self.last} free.pt\r\n".encode()
                )
            elif upper.startswith("XOVER"):
                lo, _, hi = cmd.split(" ")[1].partition("-")
                lo, hi = int(lo), int(hi or lo)
                writer.write(b"224 overview\r\n")
                for n in sorted(self.articles):
                    if lo <= n <= hi:
                        writer.write(self.articles[n].encode() + b"\r\n")
                writer.write(b".\r\n")
            elif upper.startswith("XHDR MESSAGE-ID"):
                n = int(cmd.split(" ")[2])
                writer.write(b"221 header\r\n")
                if n in self.articles:
                    msgid = self.articles[n].split("\t")[4]
                    writer.write(f"{n} {msgid}\r\n".encode())
                writer.write(b".\r\n")
            elif upper.startswith("QUIT"):
                writer.write(b"205 bye\r\n")
                await writer.drain()
                break
            else:
                writer.write(b"500 what\r\n")
            await writer.drain()
        writer.close()


@pytest.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def _configure(factory, port: int):
    async with factory() as session:
        for key, value in {
            "provider_host": "127.0.0.1",
            "provider_port": str(port),
            "provider_ssl": "no",
            "username": GOOD_USER,
            "password": GOOD_PASS,
        }.items():
            session.add(Setting(key=key, value=value))
        await session.commit()


async def test_first_run_fetches_and_stores_spots(db):
    server = FakeNewsServer(
        first=100,
        articles={
            100: _xover_line(100),
            101: _xover_line(101),
            # a non-spot usenet post in the group — must be skipped
            102: "102\tregular post\tsomeone@example.com\tFri, 24 Jul 2026 10:00:00 +0200\t<x@y>\t\t5\t1",
            103: _xover_line(103),
        },
    )
    await server.start()
    await _configure(db, server.port)
    try:
        async with db() as session:
            result = await retrieve_spots(session)
        assert result["added"] == 3
        async with db() as session:
            count = (await session.execute(select(func.count()).select_from(Spot))).scalar_one()
            assert count == 3
            spot = (
                await session.execute(
                    select(Spot).where(Spot.message_id == "<spot-100@spot.example>")
                )
            ).scalar_one()
            assert spot.title == "Spot Title 100"
            assert spot.category == 2040
            state = await session.get(UsenetState, "spots")
            assert state.cur_article == 103
            assert state.cur_message_id == "<spot-103@spot.example>"
    finally:
        await server.stop()


async def test_second_run_only_adds_new(db):
    articles = {100: _xover_line(100), 101: _xover_line(101)}
    server = FakeNewsServer(first=100, articles=articles)
    await server.start()
    await _configure(db, server.port)
    try:
        async with db() as session:
            first = await retrieve_spots(session)
        assert first["added"] == 2

        # nothing new yet
        async with db() as session:
            again = await retrieve_spots(session)
        assert again["added"] == 0

        # one new spot arrives
        articles[102] = _xover_line(102)
        async with db() as session:
            third = await retrieve_spots(session)
        assert third["added"] == 1
        async with db() as session:
            count = (await session.execute(select(func.count()).select_from(Spot))).scalar_one()
        assert count == 3
    finally:
        await server.stop()


async def test_unconfigured_provider_raises(db):
    async with db() as session:
        with pytest.raises(ValueError):
            await retrieve_spots(session)


# ---------- full-spot enrichment ----------


def _escape(data: bytes) -> bytes:
    return (
        data.replace(b"=", b"=D")
        .replace(b"\x00", b"=A")
        .replace(b"\r", b"=B")
        .replace(b"\n", b"=C")
    )


NZB_XML = b'<?xml version="1.0"?><nzb><file subject="x"><segments/></file></nzb>'
IMAGE_BYTES = b"\xff\xd8\xff\xe0FAKEJPEGDATA\x00\r\n="


class EnrichableServer(FakeNewsServer):
    """Adds HEAD (X-XML headers) and BODY (binary segments) to the fake server."""

    def __init__(self, **kw):
        super().__init__(**kw)
        chunks = [SAMPLE_XML[i : i + 80] for i in range(0, len(SAMPLE_XML), 80)]
        self.heads = {
            "<full-1@spot.example>": [f"X-XML: {c}" for c in chunks],
        }
        deflated = zlib.compress(NZB_XML)[2:-4]
        self.bodies = {
            "<nzb-part1@spot.net>": _escape(deflated[: len(deflated) // 2]),
            "<nzb-part2@spot.net>": _escape(deflated[len(deflated) // 2 :]),
            "<img-part1@spot.net>": _escape(IMAGE_BYTES),
        }

    async def _handle(self, reader, writer):
        writer.write(b"200 fake ready\r\n")
        await writer.drain()
        while True:
            line = await reader.readline()
            if not line:
                break
            cmd = line.decode().strip()
            upper = cmd.upper()
            if upper.startswith("HEAD"):
                msgid = cmd.split(" ", 1)[1]
                if msgid in self.heads:
                    writer.write(b"221 0 head\r\n")
                    for h in self.heads[msgid]:
                        writer.write(h.encode() + b"\r\n")
                    writer.write(b".\r\n")
                else:
                    writer.write(b"430 no such article\r\n")
            elif upper.startswith("BODY"):
                msgid = cmd.split(" ", 1)[1]
                if msgid in self.bodies:
                    writer.write(b"222 0 body\r\n")
                    data = self.bodies[msgid]
                    for i in range(0, len(data), 60):
                        writer.write(data[i : i + 60] + b"\r\n")
                    writer.write(b".\r\n")
                else:
                    writer.write(b"430 no such article\r\n")
            elif upper.startswith("AUTHINFO USER"):
                writer.write(b"381 pw\r\n")
            elif upper.startswith("AUTHINFO PASS"):
                writer.write(b"281 ok\r\n")
            elif upper.startswith("QUIT"):
                writer.write(b"205 bye\r\n")
                await writer.drain()
                break
            else:
                writer.write(b"500 what\r\n")
            await writer.drain()
        writer.close()


async def test_enrich_fills_description_image_and_nzb(db, tmp_path):
    from datetime import datetime

    from app.retriever import enrich_new_spots

    server = EnrichableServer()
    await server.start()
    await _configure(db, server.port)
    async with db() as session:
        session.add(
            Spot(
                message_id="<full-1@spot.example>",
                title="Great Movie 2026",
                posted_at=datetime(2026, 7, 25, tzinfo=UTC),
                category=2040,
            )
        )
        await session.commit()
    try:
        async with db() as session:
            result = await enrich_new_spots(session, images_dir=tmp_path, limit=10)
        assert result["enriched"] == 1
        async with db() as session:
            spot = (
                await session.execute(
                    select(Spot).where(Spot.message_id == "<full-1@spot.example>")
                )
            ).scalar_one()
            assert "very nice release" in spot.description
            assert spot.website == "https://example.com/info"
            assert json.loads(spot.nzb_segments) == [
                "nzb-part1@spot.net",
                "nzb-part2@spot.net",
            ]
            assert spot.enriched == 1
            assert spot.poster_url == f"/image/{spot.id}"
            assert (tmp_path / f"{spot.id}.jpg").read_bytes() == IMAGE_BYTES
        # second run: nothing left to enrich
        async with db() as session:
            again = await enrich_new_spots(session, images_dir=tmp_path, limit=10)
        assert again["enriched"] == 0
    finally:
        await server.stop()


async def test_fetch_nzb_reassembles_and_inflates(db, tmp_path):
    from app.retriever import NntpClient, fetch_binary

    server = EnrichableServer()
    await server.start()
    try:
        client = NntpClient()
        await client.connect("127.0.0.1", server.port, False, GOOD_USER, GOOD_PASS)
        nzb = await fetch_binary(
            client, ["nzb-part1@spot.net", "nzb-part2@spot.net"], compressed=True
        )
        assert nzb == NZB_XML
        await client.quit()
    finally:
        await server.stop()


async def test_subcats_are_stored_for_filtering(db):
    """Sub-dimensions (format/source/language/genre/type) must survive into the DB."""
    server = FakeNewsServer(first=100, articles={100: _xover_line(100)})
    await server.start()
    await _configure(db, server.port)
    try:
        async with db() as session:
            await retrieve_spots(session)
        async with db() as session:
            spot = (
                await session.execute(
                    select(Spot).where(Spot.message_id == "<spot-100@spot.example>")
                )
            ).scalar_one()
            # From header carries a09 (x264) b03 (Retail) c02 (NL builtin) d05
            assert spot.subcat_a == "a9|"
            assert spot.subcat_b == "b3|"
            assert spot.subcat_c == "c2|"
            assert spot.subcat_d == "d5|"
            assert spot.subcat_z == "z0|"  # derived: no TV/book/erotica markers → movie
    finally:
        await server.stop()


async def test_rescan_backfills_subcats_without_touching_enrichment(db):
    """Re-scanning must fill missing subcats on existing rows, keeping images."""
    from datetime import datetime

    server = FakeNewsServer(first=100, articles={100: _xover_line(100)})
    await server.start()
    await _configure(db, server.port)
    try:
        # simulate a row stored by the previous schema: no subcats, already enriched
        async with db() as session:
            session.add(
                Spot(
                    message_id="<spot-100@spot.example>",
                    title="Spot Title 100",
                    posted_at=datetime(2026, 7, 24, tzinfo=UTC),
                    category=2040,
                    enriched=1,
                    poster_url="/image/1",
                )
            )
            await session.commit()

        async with db() as session:
            result = await retrieve_spots(session)
        assert result["added"] == 0
        assert result["backfilled"] == 1

        async with db() as session:
            spot = (
                await session.execute(
                    select(Spot).where(Spot.message_id == "<spot-100@spot.example>")
                )
            ).scalar_one()
            assert spot.subcat_a == "a9|"
            assert spot.subcat_z == "z0|"
            assert spot.enriched == 1  # enrichment preserved
            assert spot.poster_url == "/image/1"
    finally:
        await server.stop()


# ---------- full-history backfill (retrieve_all) ----------


async def _count(db) -> int:
    async with db() as session:
        return (await session.execute(select(func.count()).select_from(Spot))).scalar_one()


def _history(n: int = 100) -> FakeNewsServer:
    return FakeNewsServer(first=100, articles={i: _xover_line(i) for i in range(100, 100 + n)})


async def test_without_retrieve_all_old_articles_stay_unfetched(db):
    server = _history()
    await server.start()
    await _configure(db, server.port)
    try:
        async with db() as session:
            result = await retrieve_spots(session, max_backfill=10)
        assert result["added"] == 11
        assert result["backward_scanned"] == 0
    finally:
        await server.stop()


async def test_retrieve_all_walks_back_to_the_servers_oldest_article(db, monkeypatch):
    monkeypatch.setattr("app.retriever.INCREMENT", 10)  # force several backward chunks
    server = _history()
    await server.start()
    await _configure(db, server.port)
    try:
        async with db() as session:
            await retrieve_spots(session, max_backfill=10)  # an existing, shallow install
        assert await _count(db) == 11

        async with db() as session:
            result = await retrieve_spots(session, max_backfill=10, retrieve_all=True)
        assert await _count(db) == 100  # nothing duplicated, nothing missed
        assert result["floor"] == 100

        async with db() as session:  # converged: nothing left to walk
            again = await retrieve_spots(session, max_backfill=10, retrieve_all=True)
        assert again["added"] == 0
        assert again["backward_scanned"] == 0
    finally:
        await server.stop()


async def test_retrieve_all_on_a_fresh_install_gets_everything(db):
    server = _history()
    await server.start()
    await _configure(db, server.port)
    try:
        async with db() as session:
            await retrieve_spots(session, max_backfill=10, retrieve_all=True)
        assert await _count(db) == 100
    finally:
        await server.stop()


async def test_a_renumbered_floor_is_reseeded_not_trusted(db):
    server = _history()
    await server.start()
    await _configure(db, server.port)
    try:
        async with db() as session:
            await retrieve_spots(session, max_backfill=10)
        async with db() as session:
            floor = await session.get(UsenetState, "spots_floor")
            floor.cur_article, floor.cur_message_id = 50, "<not-there@x>"
            await session.commit()
        async with db() as session:
            await retrieve_spots(session, max_backfill=10, retrieve_all=True)
        assert await _count(db) == 100
    finally:
        await server.stop()


async def test_backfill_skips_spots_older_than_the_retention_cutoff(db):
    from datetime import datetime

    server = _history(20)  # all dated 2026-07-24
    await server.start()
    await _configure(db, server.port)
    try:
        async with db() as session:
            result = await retrieve_spots(
                session, retrieve_all=True, min_posted=datetime(2027, 1, 1, tzinfo=UTC)
            )
        # fresh install: the forward pass stores the recent window, the backward pass is filtered
        assert result["added"] == 20  # forward pass is unfiltered by design
        async with db() as session:
            await session.execute(Spot.__table__.delete())
            await session.commit()
            floor = await session.get(UsenetState, "spots_floor")
            floor.cur_article, floor.cur_message_id = 120, ""
            await session.commit()
        async with db() as session:
            again = await retrieve_spots(
                session, retrieve_all=True, min_posted=datetime(2027, 1, 1, tzinfo=UTC)
            )
        assert again["added"] == 0
    finally:
        await server.stop()


async def test_backward_pass_ends_even_if_the_server_ignores_the_range(db, monkeypatch):
    from app.retriever import NntpClient

    server = _history()
    await server.start()
    await _configure(db, server.port)
    real_xover = NntpClient.xover

    async def sloppy(self, lo, hi):  # always answers with the whole group
        return await real_xover(self, 100, 199)

    try:
        async with db() as session:
            await retrieve_spots(session, max_backfill=10)
        monkeypatch.setattr(NntpClient, "xover", sloppy)
        async with db() as session:
            result = await asyncio.wait_for(
                retrieve_spots(session, max_backfill=10, retrieve_all=True), timeout=10
            )
        assert result["floor"] == 100
    finally:
        await server.stop()


# ---------- day limit and progress status ----------


def _dated_line(n: int, days_ago: int, now) -> str:
    from datetime import timedelta
    from email.utils import format_datetime

    stamp = format_datetime(now - timedelta(days=days_ago))
    return _xover_line(n).replace("Fri, 24 Jul 2026 10:00:00 +0200", stamp)


async def test_full_history_stops_at_the_day_limit_and_stays_stopped(db, monkeypatch):
    from datetime import datetime

    from app.progress import load_status

    monkeypatch.setattr("app.retriever.INCREMENT", 10)
    now = datetime.now(UTC)
    # article n is (199 - n) days old: 100 is 99 days old, 199 is today
    server = FakeNewsServer(
        first=100, articles={n: _dated_line(n, 199 - n, now) for n in range(100, 200)}
    )
    await server.start()
    await _configure(db, server.port)
    try:
        async with db() as session:
            result = await retrieve_spots(
                session, max_backfill=10, retrieve_all=True, max_age_days=30
            )
        assert result["floor"] == 159  # stopped well before the server's oldest article (100)
        assert await _count(db) == 30  # nothing older than 30 days was stored
        async with db() as session:
            oldest = (await session.execute(select(func.min(Spot.posted_at)))).scalar_one()
            status = await load_status(session)
        assert (now - oldest.replace(tzinfo=UTC)).days <= 30
        assert status["days_reached_for"] == 30

        async with db() as session:  # the next cycle does not re-walk
            again = await retrieve_spots(
                session, max_backfill=10, retrieve_all=True, max_age_days=30
            )
        assert again["backward_scanned"] == 0

        async with db() as session:  # raising the limit resumes the walk
            more = await retrieve_spots(
                session, max_backfill=10, retrieve_all=True, max_age_days=60
            )
        assert more["backward_scanned"] > 0 and more["floor"] < 159
    finally:
        await server.stop()


async def test_the_server_range_and_oldest_date_are_remembered(db):
    from datetime import datetime

    from app.progress import history_percent, load_status

    now = datetime.now(UTC)
    server = FakeNewsServer(
        first=100, articles={n: _dated_line(n, 199 - n, now) for n in range(100, 200)}
    )
    await server.start()
    await _configure(db, server.port)
    try:
        async with db() as session:
            await retrieve_spots(session, max_backfill=10, retrieve_all=True)
            status = await load_status(session)
        assert status["group_first"] == 100 and status["group_last"] == 199
        oldest = datetime.fromisoformat(status["oldest_date"])
        assert 98 <= (now - oldest).days <= 100
        assert history_percent(status) == 100.0  # reached the server's first article
    finally:
        await server.stop()


async def test_days_mode_first_run_walks_back_that_many_days(db, monkeypatch):
    from datetime import datetime

    from app.settings_store import backfill_plan

    monkeypatch.setattr("app.retriever.INCREMENT", 10)
    now = datetime.now(UTC)
    server = FakeNewsServer(
        first=100, articles={n: _dated_line(n, 199 - n, now) for n in range(100, 200)}
    )
    await server.start()
    await _configure(db, server.port)
    prefs = {
        "retrieve_all": False,
        "retrieve_days": 0,
        "retrieve_backfill": 20,
        "retrieve_backfill_unit": "days",
    }
    try:
        async with db() as session:
            plan = backfill_plan(prefs)
            plan["max_backfill"] = 5  # the seed window is tiny in this 100-article group
            await retrieve_spots(session, **plan)
        async with db() as session:
            oldest = (await session.execute(select(func.min(Spot.posted_at)))).scalar_one()
        assert (now - oldest.replace(tzinfo=UTC)).days <= 20
        assert 15 <= await _count(db) <= 21  # about 20 days of spots, not the whole group
    finally:
        await server.stop()


# ---------- parallel connections and lazy enrichment ----------


async def _set(factory, **values):
    async with factory() as s:
        for k, v in values.items():
            s.add(Setting(key=k, value=v))
        await s.commit()


async def test_history_walk_uses_parallel_connections_and_loses_nothing(db, monkeypatch):
    monkeypatch.setattr("app.retriever.INCREMENT", 10)
    server = _history()
    await server.start()
    await _configure(db, server.port)
    await _set(db, connections="4")
    try:
        async with db() as session:
            result = await retrieve_spots(session, max_backfill=10, retrieve_all=True)
        assert await _count(db) == 100 and result["floor"] == 100
        assert server.peak >= 3  # several windows were really fetched at once
        assert server.concurrent == 0  # and every connection was closed again
    finally:
        await server.stop()


async def test_a_provider_that_refuses_extra_connections_just_means_fewer_workers(db, monkeypatch):
    monkeypatch.setattr("app.retriever.INCREMENT", 10)
    server = FakeNewsServer(
        first=100, articles={i: _xover_line(i) for i in range(100, 200)}, max_clients=2
    )
    await server.start()
    await _configure(db, server.port)
    await _set(db, connections="6")
    try:
        async with db() as session:
            await retrieve_spots(session, max_backfill=10, retrieve_all=True)
        assert await _count(db) == 100 and server.peak == 2
    finally:
        await server.stop()


async def test_the_day_limit_still_stops_the_parallel_walk_at_the_right_chunk(db, monkeypatch):
    from datetime import datetime

    monkeypatch.setattr("app.retriever.INCREMENT", 10)
    now = datetime.now(UTC)
    server = FakeNewsServer(
        first=100, articles={n: _dated_line(n, 199 - n, now) for n in range(100, 200)}
    )
    await server.start()
    await _configure(db, server.port)
    await _set(db, connections="4")
    try:
        async with db() as session:
            result = await retrieve_spots(
                session, max_backfill=10, retrieve_all=True, max_age_days=30
            )
        assert result["floor"] >= 150  # stopped near day 30, not at the server's first article
        assert await _count(db) == 30
    finally:
        await server.stop()


async def test_lazy_enrichment_skips_old_spots_until_they_are_opened(db):
    from datetime import datetime, timedelta

    from app.retriever import enrich_new_spots

    server = FakeNewsServer(first=1, articles={})
    await server.start()
    await _configure(db, server.port)
    now = datetime.now(UTC)
    async with db() as s:
        s.add(Spot(message_id="<young@x>", title="Young", posted_at=now - timedelta(days=2)))
        s.add(Spot(message_id="<old@x>", title="Old", posted_at=now - timedelta(days=400)))
        await s.commit()
    try:
        async with db() as s:  # nothing is on the fake server, so each attempt marks "failed" (2)
            result = await enrich_new_spots(
                s, images_dir="/tmp/x-unused", newer_than=now - timedelta(days=30)
            )
        assert result["failed"] == 1  # only the young spot was attempted
        async with db() as s:
            rows = {r.title: r.enriched for r in (await s.execute(select(Spot))).scalars().all()}
        assert rows == {"Young": 2, "Old": 0}
        async with db() as s:  # opening the old spot enriches it regardless of age
            old = (await s.execute(select(Spot).where(Spot.title == "Old"))).scalar_one()
            await enrich_new_spots(s, images_dir="/tmp/x-unused", only_id=old.id)
            assert old.enriched == 2
    finally:
        await server.stop()
