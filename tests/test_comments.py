"""Read-only Spotnet comments: parsing, linking, retrieval, on-demand bodies, display."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.comments import (
    comment_counts,
    fetch_comment_bodies,
    parse_comment_row,
    retrieve_comments,
)
from app.models import Base, Comment, Setting, UsenetState
from tests.test_retriever import GOOD_PASS, GOOD_USER, FakeNewsServer


def _row(n: int = 1, mid: str | None = None, **over) -> dict:
    row = {
        "Number": n,
        "Subject": "Re: whatever",
        "From": "Alice <key@host>",
        "Date": "Fri, 24 Jul 2026 10:00:00 +0200",
        "Message-ID": mid if mid is not None else f"<spot{n}.5.a.b@spot.example>",
    }
    row.update(over)
    return row


def _line(n: int, mid: str | None = None) -> str:
    r = _row(n, mid)
    return "\t".join([str(n), r["Subject"], r["From"], r["Date"], r["Message-ID"], "", "100", "1"])


def test_a_comment_links_to_its_spot_by_message_id():
    c = parse_comment_row(_row(mid="<abc123.7.x.y@spot.example>"))
    assert c["spot_message_id"] == "<abc123@spot.example>"
    assert c["poster"] == "Alice"
    assert c["posted_at"].tzinfo is not None


@pytest.mark.parametrize(
    "mid",
    ["<nodots@spot.example>", "<a.b@>", "a.b@spot.example", "<a b.c@x.y>", "<.b@x.y>", ""],
)
def test_malformed_message_ids_are_rejected(mid):
    assert parse_comment_row(_row(mid=mid)) is None


def test_poster_control_characters_are_stripped_and_capped():
    c = parse_comment_row(_row(**{"From": "Eve\x00\x1b[31m" + "x" * 200 + " <k@h>"}))
    assert "\x00" not in c["poster"] and "\x1b" not in c["poster"]
    assert len(c["poster"]) <= 80


@pytest.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


class CommentServer(FakeNewsServer):
    """FakeNewsServer plus BODY <msgid>."""

    def __init__(self, articles, bodies):
        super().__init__(first=1, articles=articles)
        self.bodies = bodies

    async def _handle(self, reader, writer):
        # Intercept BODY, delegate everything else to the parent protocol handler.
        orig_readline = reader.readline

        async def readline():
            line = await orig_readline()
            if line.upper().startswith(b"BODY "):
                mid = line.decode().strip().split(" ", 1)[1]
                if mid in self.bodies:
                    writer.write(b"222 0 body\r\n")
                    writer.write(self.bodies[mid] + b"\r\n.\r\n")
                else:
                    writer.write(b"430 no such article\r\n")
                await writer.drain()
                return await readline()
            return line

        reader.readline = readline
        await super()._handle(reader, writer)


async def _configure(factory, port):
    async with factory() as session:
        for k, v in {
            "provider_host": "127.0.0.1",
            "provider_port": str(port),
            "provider_ssl": "no",
            "username": GOOD_USER,
            "password": GOOD_PASS,
        }.items():
            session.add(Setting(key=k, value=v))
        await session.commit()


async def test_retrieval_stores_headers_once_and_counts_per_spot(db):
    articles = {
        1: _line(1, "<s1.1.a.b@spot.example>"),
        2: _line(2, "<s1.2.c.d@spot.example>"),
        3: _line(3, "<s2.1.e.f@spot.example>"),
        4: "4\tjunk\tsomeone@x\tFri, 24 Jul 2026 10:00:00 +0200\t<junk@x>\t\t1\t1",
    }
    server = CommentServer(articles, {})
    await server.start()
    await _configure(db, server.port)
    try:
        async with db() as session:
            result = await retrieve_comments(session, "free.usenet", 100)
        assert result["added"] == 3 and result["invalid"] == 1
        async with db() as session:
            again = await retrieve_comments(session, "free.usenet", 100)
            counts = await comment_counts(
                session, ["<s1@spot.example>", "<s2@spot.example>", "<none@x.y>"]
            )
        assert again["added"] == 0
        assert counts == {"<s1@spot.example>": 2, "<s2@spot.example>": 1}
    finally:
        await server.stop()


async def test_a_bad_group_name_never_reaches_the_wire(db):
    async with db() as session:
        with pytest.raises(ValueError):
            await retrieve_comments(session, "free.usenet\r\nQUIT", 100)


async def test_retention_cutoff_skips_old_comments(db):
    server = CommentServer({1: _line(1)}, {})
    await server.start()
    await _configure(db, server.port)
    try:
        async with db() as session:
            result = await retrieve_comments(
                session, "free.usenet", 100, min_posted=datetime(2027, 1, 1, tzinfo=UTC)
            )
        assert result["added"] == 0
    finally:
        await server.stop()


async def test_bodies_load_on_demand_sanitised_truncated_and_gone_ones_marked(db):
    big = b"x" * 20000
    bodies = {
        "<s1.1.a.b@spot.example>": "héllo <b>bold</b>\x00\x1b!".encode(),
        "<s1.2.c.d@spot.example>": big,
    }
    articles = {
        1: _line(1, "<s1.1.a.b@spot.example>"),
        2: _line(2, "<s1.2.c.d@spot.example>"),
        3: _line(3, "<s1.3.e.f@spot.example>"),  # no body on the server
    }
    server = CommentServer(articles, bodies)
    await server.start()
    await _configure(db, server.port)
    try:
        async with db() as session:
            await retrieve_comments(session, "free.usenet", 100)
            stored = await fetch_comment_bodies(session, "<s1@spot.example>")
            rows = {
                c.message_id: c for c in (await session.execute(select(Comment))).scalars().all()
            }
        assert stored == 2
        first = rows["<s1.1.a.b@spot.example>"]
        assert first.body == "héllo <b>bold</b>!" and first.body_state == 1
        assert len(rows["<s1.2.c.d@spot.example>"].body) == 10 * 1024
        assert rows["<s1.3.e.f@spot.example>"].body_state == 2
    finally:
        await server.stop()


async def test_the_comments_bookmark_survives_between_runs(db):
    server = CommentServer({1: _line(1)}, {})
    await server.start()
    await _configure(db, server.port)
    try:
        async with db() as session:
            await retrieve_comments(session, "free.usenet", 100)
            state = await session.get(UsenetState, "comments")
        assert state.cur_article == 1
        n = await _count(db)
        assert n == 1
    finally:
        await server.stop()


async def _count(db):
    async with db() as session:
        return (await session.execute(select(func.count()).select_from(Comment))).scalar_one()


async def test_an_oversized_body_is_cut_off_while_reading(db):
    from app.comments import MAX_BODY_WIRE

    huge = (b"y" * 900 + b"\r\n") * (MAX_BODY_WIRE // 900 + 50)
    articles = {1: _line(1, "<s1.1.a.b@spot.example>")}
    server = CommentServer(articles, {"<s1.1.a.b@spot.example>": huge})
    await server.start()
    await _configure(db, server.port)
    try:
        async with db() as session:
            await retrieve_comments(session, "free.usenet", 100)
            assert await fetch_comment_bodies(session, "<s1@spot.example>") == 0
            c = (await session.execute(select(Comment))).scalar_one()
        assert c.body == "" and c.body_state == 2
    finally:
        await server.stop()
