"""Importing a classic Spotweb database (SQLite stand-in with Spotweb's real table layout)."""

import json

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import Base, Comment, Download, Spot, Spotter
from app.spotnet import to_newznab
from app.spotweb_import import comment_row, main, run_import, spot_row
from tests.test_spotnet import SAMPLE_XML

SCHEMA = """
CREATE TABLE spots (id INTEGER PRIMARY KEY, messageid TEXT, poster TEXT, title TEXT, tag TEXT,
  category INTEGER, subcata TEXT, subcatb TEXT, subcatc TEXT, subcatd TEXT, subcatz TEXT,
  stamp INTEGER, filesize INTEGER, spotterid TEXT);
CREATE TABLE spotsfull (id INTEGER PRIMARY KEY, messageid TEXT, fullxml TEXT);
CREATE TABLE commentsxover (id INTEGER PRIMARY KEY, messageid TEXT, nntpref TEXT, spotrating INTEGER, stamp INTEGER);
CREATE TABLE commentsfull (id INTEGER PRIMARY KEY, messageid TEXT, fromhdr TEXT, stamp INTEGER, body TEXT);
CREATE TABLE spotstatelist (id INTEGER PRIMARY KEY, messageid TEXT, ouruserid INTEGER, download INTEGER, watch INTEGER, seen INTEGER);
CREATE TABLE spotteridblacklist (id INTEGER PRIMARY KEY, spotterid TEXT, ouruserid INTEGER, idtype INTEGER);
"""

XML_WITH_IMDB = SAMPLE_XML.replace(
    "<Website>https://example.com/info</Website>",
    "<Website>http://www.imdb.com/title/tt0113277/</Website>",
)
XML_NO_IMAGE = XML_WITH_IMDB.replace(
    '<Image Width="300" Height="450"><Segment>img-part1@spot.net</Segment></Image>', ""
)


@pytest.fixture
def spotweb(tmp_path):
    path = tmp_path / "spotweb.db"
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as conn:
        for stmt in filter(None, (s.strip() for s in SCHEMA.split(";"))):
            conn.execute(text(stmt))
        rows = [
            (
                1,
                "abc1@spot.net",
                "Nick",
                "Dune 2024 2160p",
                "tag",
                0,
                "a9|",
                "b3|",
                "",
                "d5|",
                "z0|",
                1753500000,
                5_000_000_000,
            ),
            (
                2,
                "abc2@spot.net",
                "Nick",
                "Silo S03E01",
                "",
                0,
                "a9|",
                "",
                "",
                "",
                "z1|",
                1753500100,
                900_000_000,
            ),
            (
                3,
                "abc3@spot.net",
                "Nick",
                "",
                "",
                0,
                "",
                "",
                "",
                "",
                "",
                1753500200,
                1,
            ),  # no title: unusable
            (
                4,
                "bad id with spaces",
                "Nick",
                "Weird",
                "",
                0,
                "",
                "",
                "",
                "",
                "z0|",
                1753500300,
                1,
            ),  # bad id
        ]
        for r in rows:
            conn.execute(
                text("INSERT INTO spots VALUES (:a,:b,:c,:d,:e,:f,:g,:h,:i,:j,:k,:l,:m,'SPID')"),
                dict(zip("abcdefghijklm", r, strict=True)),
            )
        conn.execute(
            text("INSERT INTO spotsfull VALUES (1,'abc1@spot.net',:x)"), {"x": XML_WITH_IMDB}
        )
        conn.execute(
            text("INSERT INTO spotsfull VALUES (2,'abc2@spot.net',:x)"), {"x": XML_NO_IMAGE}
        )
        conn.execute(
            text(
                "INSERT INTO commentsxover VALUES (1,'abc1.5.x.y@spot.net','abc1@spot.net',5,1753501000)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO commentsfull VALUES (1,'abc1.5.x.y@spot.net','Al <k@h>',1753501000,'great release')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO commentsxover VALUES (2,'abc1.0.z.w@spot.net','abc1@spot.net',0,1753501100)"
            )
        )  # no full row
        conn.execute(
            text("INSERT INTO spotstatelist VALUES (1,'abc1@spot.net',1,1753502000,NULL,NULL)")
        )
        conn.execute(
            text("INSERT INTO spotstatelist VALUES (2,'abc1@spot.net',2,1753502500,NULL,NULL)")
        )  # two users
        conn.execute(
            text("INSERT INTO spotstatelist VALUES (3,'abc2@spot.net',1,NULL,1753502000,NULL)")
        )  # only watched
        conn.execute(text("INSERT INTO spotteridblacklist VALUES (1,'SPAMER',-1,1)"))
        conn.execute(text("INSERT INTO spotteridblacklist VALUES (2,'GOODIE',-1,2)"))
    yield engine
    engine.dispose()


@pytest.fixture
async def target():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def test_everything_is_mapped_into_our_model(spotweb, target):
    result = await run_import(
        spotweb, target, batch=2, say=lambda m: None
    )  # batch 2: several pages
    assert result["added"] == {"spots": 2, "downloads": 1, "spotters": 2, "comments": 2}
    async with target() as s:
        spots = {r.title: r for r in (await s.execute(select(Spot))).scalars().all()}
        assert set(spots) == {"Dune 2024 2160p", "Silo S03E01"}  # unusable rows skipped
        dune = spots["Dune 2024 2160p"]
        assert dune.message_id == "<abc1@spot.net>"  # Spotweb stores ids without <>
        assert dune.category == to_newznab(
            0, {"a": ["a9"], "b": ["b3"], "c": [], "d": ["d5"], "z": ["z0"]}
        )
        assert (dune.subcat_a, dune.subcat_b, dune.subcat_d, dune.subcat_z) == (
            "a9|",
            "b3|",
            "d5|",
            "z0|",
        )
        assert dune.size == 5_000_000_000 and dune.posted_at.year == 2025
        assert dune.imdb_id == "0113277" and "nice release" in dune.description
        assert json.loads(dune.nzb_segments) == ["nzb-part1@spot.net", "nzb-part2@spot.net"]
        assert dune.enriched == 0  # it has a cover we did not copy: fetched later
        assert spots["Silo S03E01"].enriched == 1  # complete without an image
        assert (dune.adult, dune.title_key) == (0, "dune 2024")  # derived values set on the way in
        assert (dune.verified, dune.spotter_id) == (0, "")  # re-verified from a real header later
        dl = (await s.execute(select(Download))).scalar_one()
        assert dl.message_id == "<abc1@spot.net>" and dl.source == "spotweb" and dl.count == 1
        assert dl.first_at.year == 2025  # the earliest of the two users' downloads
        sp = {r.spotter_id: r.status for r in (await s.execute(select(Spotter))).scalars().all()}
        assert sp == {"SPAMER": "blocked", "GOODIE": "trusted"}
        comments = {r.message_id: r for r in (await s.execute(select(Comment))).scalars().all()}
        full = comments["<abc1.5.x.y@spot.net>"]
        assert (full.poster, full.body, full.body_state, full.spot_message_id) == (
            "Al",
            "great release",
            1,
            "<abc1@spot.net>",
        )
        assert comments["<abc1.0.z.w@spot.net>"].body_state == 0  # header only: body loads on view


async def test_the_import_is_repeatable_and_resumable(spotweb, target):
    await run_import(spotweb, target, batch=1, say=lambda m: None)
    again = await run_import(spotweb, target, batch=1, say=lambda m: None)
    assert again["added"] == {}  # nothing new, nothing duplicated
    async with target() as s:
        assert (await s.execute(select(func.count()).select_from(Spot))).scalar_one() == 2


async def test_a_spotweb_blocklist_entry_hides_spots_posted_under_that_id(spotweb, target):
    from app.spots import search_spots

    await run_import(spotweb, target, say=lambda m: None)
    async with target() as s:
        s.add(
            Spot(
                message_id="<s@x>",
                title="Spam post",
                posted_at=(await s.execute(select(Spot.posted_at))).scalars().first(),
                spotter_id="SPAMERxyz12345",
            )
        )  # one of our own (longer) ids for the same poster
        await s.commit()
        titles = {r.title for r in (await search_spots(s))[0]}
    assert "Spam post" not in titles  # short imported id blocks by prefix


def test_row_builders_reject_unusable_rows():
    assert spot_row({"messageid": "", "title": "x", "stamp": 1, "category": 0}) is None
    assert spot_row({"messageid": "a@b", "title": "x", "stamp": "soon", "category": 0}) is None
    assert spot_row({"messageid": "a@b", "title": "x", "stamp": 1, "category": "zero"}) is None
    assert comment_row({"messageid": "a.1@b", "nntpref": "", "stamp": 1}) is None


def test_the_cli_reports_an_unreadable_source_without_echoing_credentials(capsys, tmp_path):
    code = main(["--url", f"sqlite:///{tmp_path}/missing-dir/none.db"])
    assert code == 2
    out = capsys.readouterr().err
    assert "Cannot read a Spotweb database" in out and "missing-dir" not in out
