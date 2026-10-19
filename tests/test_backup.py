"""Export/import of the index: round trip, merge safety, and hostile files."""

import gzip
import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.backup import export_chunks, import_file
from app.models import Base, Comment, Download, Setting, Spot, Spotter, UsenetState

NOW = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)


async def _factory():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False), engine


@pytest.fixture
async def source():
    factory, engine = await _factory()
    async with factory() as s:
        for i in range(5):
            s.add(
                Spot(
                    message_id=f"<s{i}@x>",
                    title=f"Title {i}",
                    posted_at=NOW,
                    category=2040,
                    subcat_z="z0|",
                    description="kept",
                    from_hdr="Nick <k@h>",
                    verified=1,
                    spotter_id="SID123",
                    poster_url="/image/9" if i == 0 else "https://img/x.jpg",
                    enriched=1,
                )
            )
        s.add(
            Comment(
                message_id="<s1.1.a.b@x>",
                spot_message_id="<s1@x>",
                poster="Al",
                posted_at=NOW,
                body="hi",
                body_state=1,
            )
        )
        s.add(UsenetState(infotype="spots", cur_article=500, cur_message_id="<s4@x>"))
        s.add(Spotter(spotter_id="SPAMID", status="blocked", note="spam"))
        s.add(Download(message_id="<s2@x>", first_at=NOW, last_at=NOW, count=3, source="send"))
        s.add(Setting(key="password", value="usenet-secret"))
        s.add(Setting(key="provider_host", value="news.example.com"))
        await s.commit()
    yield factory
    await engine.dispose()


async def _export(factory, tmp_path) -> str:
    path = tmp_path / "out.gz"
    with open(path, "wb") as fh:
        async for chunk in export_chunks(factory):
            fh.write(chunk)
    return str(path)


async def _count(factory, model) -> int:
    async with factory() as s:
        return (await s.execute(select(func.count()).select_from(model))).scalar_one()


async def test_round_trip_into_a_fresh_install(source, tmp_path):
    path = await _export(source, tmp_path)
    target, engine = await _factory()
    try:
        result = await import_file(target, path)
        assert result["added"] == {
            "spots": 5,
            "comments": 1,
            "downloads": 1,
            "spotters": 1,
            "usenet_state": 1,
        }
        assert await _count(target, Spot) == 5
        async with target() as s:
            dl = (await s.execute(select(Download))).scalar_one()
            assert (dl.message_id, dl.count, dl.source) == ("<s2@x>", 3, "send")
            sp = (await s.execute(select(Spotter))).scalar_one()
            assert (sp.spotter_id, sp.status) == ("SPAMID", "blocked")
            signed = (await s.execute(select(Spot).where(Spot.message_id == "<s3@x>"))).scalar_one()
            assert (signed.verified, signed.spotter_id, signed.from_hdr) == (
                1,
                "SID123",
                "Nick <k@h>",
            )
            state = await s.get(UsenetState, "spots")
            assert state.cur_article == 500  # no need to re-read the group
            cover_lost = (
                await s.execute(select(Spot).where(Spot.message_id == "<s0@x>"))
            ).scalar_one()
            remote = (await s.execute(select(Spot).where(Spot.message_id == "<s1@x>"))).scalar_one()
        assert cover_lost.enriched == 0 and cover_lost.poster_url == ""  # re-fetched later
        assert remote.enriched == 1 and remote.description == "kept"
    finally:
        await engine.dispose()


async def test_importing_twice_adds_nothing_and_never_overwrites(source, tmp_path):
    path = await _export(source, tmp_path)
    target, engine = await _factory()
    try:
        await import_file(target, path)
        async with target() as s:
            (
                await s.execute(select(Spot).where(Spot.message_id == "<s2@x>"))
            ).scalar_one().title = "Local edit"
            (await s.get(UsenetState, "spots")).cur_article = 900
            await s.commit()
        again = await import_file(target, path)
        assert again["added"] == {} and again["skipped"] >= 7
        async with target() as s:
            spot = (await s.execute(select(Spot).where(Spot.message_id == "<s2@x>"))).scalar_one()
            assert spot.title == "Local edit"
            assert (await s.get(UsenetState, "spots")).cur_article == 900
    finally:
        await engine.dispose()


async def test_credentials_are_never_exported(source, tmp_path):
    path = await _export(source, tmp_path)
    text = gzip.open(path, "rt").read()
    assert "usenet-secret" not in text and "news.example.com" not in text


async def test_a_file_that_is_not_an_export_is_refused(tmp_path):
    target, engine = await _factory()
    try:
        bad = tmp_path / "bad.gz"
        bad.write_bytes(gzip.compress(b'{"format":"something-else","version":1}\n'))
        with pytest.raises(ValueError):
            await import_file(target, str(bad))
        plain = tmp_path / "plain.gz"
        plain.write_bytes(b"not gzip at all")
        with pytest.raises(ValueError):
            await import_file(target, str(plain))
        future = tmp_path / "future.gz"
        future.write_bytes(gzip.compress(b'{"format":"spotwebng-export","version":99}\n'))
        with pytest.raises(ValueError):
            await import_file(target, str(future))
    finally:
        await engine.dispose()


async def test_hostile_rows_are_clamped_or_skipped(tmp_path):
    target, engine = await _factory()
    try:
        lines = [
            {"format": "spotwebng-export", "version": 1},
            {
                "t": "spots",
                "r": {"message_id": "<ok@x>", "title": "T" * 5000, "posted_at": NOW.isoformat()},
            },
            {
                "t": "spots",
                "r": {"message_id": "<nodate@x>", "title": "x"},
            },  # required column missing
            {
                "t": "spots",
                "r": {"message_id": "<baddate@x>", "title": "x", "posted_at": "yesterday-ish"},
            },
            {
                "t": "spots",
                "r": {
                    "message_id": "<badint@x>",
                    "title": "x",
                    "posted_at": NOW.isoformat(),
                    "size": "lots",
                },
            },
            {"t": "nonsense", "r": {}},
            {"t": "spots", "r": "not a dict"},
        ]
        path = tmp_path / "evil.gz"
        path.write_bytes(
            gzip.compress(("\n".join(json.dumps(x) for x in lines) + "\nnot json\n").encode())
        )
        result = await import_file(target, str(path))
        assert result["added"] == {"spots": 1}
        async with target() as s:
            spot = (await s.execute(select(Spot))).scalar_one()
        assert len(spot.title) == 512  # clamped to the column length
    finally:
        await engine.dispose()


async def test_imported_identifiers_and_urls_are_revalidated(tmp_path):
    target, engine = await _factory()
    try:
        ok = {"posted_at": NOW.isoformat(), "title": "t"}
        lines = [
            {"format": "spotwebng-export", "version": 1},
            # CRLF in a message-id would become an extra NNTP command at BODY/HEAD time
            {"t": "spots", "r": {**ok, "message_id": "<a@x>\r\nQUIT"}},
            {
                "t": "comments",
                "r": {
                    "message_id": "<s.1.a@x>\r\nQUIT",
                    "spot_message_id": "<s@x>",
                    "posted_at": NOW.isoformat(),
                },
            },
            {
                "t": "comments",
                "r": {
                    "message_id": "<s.1.a@x>",
                    "spot_message_id": "<s@x> bad",
                    "posted_at": NOW.isoformat(),
                },
            },
            {
                "t": "spots",
                "r": {
                    **ok,
                    "message_id": "<good@x>",
                    "website": "javascript:alert(1)",
                    "poster_url": "javascript:alert(1)",
                    "nzb_ref": "<a@x>\nQUIT",
                    "nzb_segments": json.dumps(["fine@x", "bad\r\nQUIT"]),
                    "description": "line1\nline2\x00",
                    "poster": "Mal\x1b[2Jlory",
                },
            },
        ]
        path = tmp_path / "hostile.gz"
        path.write_bytes(gzip.compress(("\n".join(json.dumps(x) for x in lines) + "\n").encode()))
        result = await import_file(target, str(path))
        assert result["added"] == {"spots": 1}
        assert await _count(target, Comment) == 0
        async with target() as s:
            spot = (await s.execute(select(Spot))).scalar_one()
        assert spot.website == "" and spot.poster_url == ""
        assert spot.nzb_ref == "" and spot.nzb_segments == ""
        assert spot.description == "line1\nline2" and spot.poster == "Mal[2Jlory"
    finally:
        await engine.dispose()


async def test_a_decompression_bomb_is_refused(tmp_path):
    target, engine = await _factory()
    try:
        filler = json.dumps({"t": "nonsense", "r": {"pad": "A" * 1_000_000}}) + "\n"
        body = '{"format":"spotwebng-export","version":1}\n' + filler * 400  # ~400 MB expanded
        path = tmp_path / "bomb.gz"
        path.write_bytes(gzip.compress(body.encode(), compresslevel=9))
        with pytest.raises(ValueError, match="expands"):
            await import_file(target, str(path))
    finally:
        await engine.dispose()


async def test_nntp_commands_cannot_carry_line_breaks():
    from app.retriever import NntpClient, NntpError

    client = NntpClient()
    client.writer = object()  # never reached: the guard fires first
    with pytest.raises(NntpError):
        await client.command("BODY <a@x>\r\nQUIT")
