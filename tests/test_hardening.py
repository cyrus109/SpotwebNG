"""Regression tests for the 2026-07-26 audit findings.

Each test names the finding it locks down. These must never go green-to-red.
"""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import get_session
from app.main import app
from app.models import ApiUser, Base, Setting, Spot

NOW = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)


@pytest.fixture
async def client():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def override():
        async with factory() as s:
            yield s

    app.dependency_overrides[get_session] = override
    async with factory() as s:
        s.add(ApiUser(name="default", api_key="k" * 32))
        s.add(Setting(key="provider_host", value="news.example.com"))
        s.add(Setting(key="password", value="s3cret-usenet-pw"))
        s.add(Setting(key="username", value="alice"))
        for i in range(5):
            s.add(
                Spot(
                    message_id=f"<h{i}@x>",
                    title=f"Spot {i}",
                    posted_at=NOW - timedelta(hours=i),
                    category=2040,
                    spot_category=0,
                    subcat_z="z0|",
                )
            )
        await s.commit()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c, factory
    app.dependency_overrides.clear()
    await engine.dispose()


# --- SECURITY ---------------------------------------------------------


async def test_setup_page_never_echoes_the_provider_password(client):
    """BLOCKER 1: /setup rendered the Usenet password into value=""."""
    c, _ = client
    resp = await c.get("/setup")
    assert resp.status_code == 200
    assert "s3cret-usenet-pw" not in resp.text


async def test_saving_setup_without_a_password_keeps_the_stored_one(client):
    """A blank password field must mean 'unchanged', not 'wipe it'."""
    c, factory = client
    await c.post(
        "/setup",
        data={
            "provider_host": "news.example.com",
            "provider_port": "563",
            "provider_ssl": "yes",
            "username": "alice",
            "password": "",
            "connections": "4",
        },
    )
    async with factory() as s:
        assert (await s.get(Setting, "password")).value == "s3cret-usenet-pw"


async def test_search_limit_cannot_be_negative(client):
    """MAJOR 5: limit=-1 rendered LIMIT -1, i.e. the entire table."""
    c, _ = client
    resp = await c.get(f"/api?t=search&apikey={'k' * 32}&limit=-1")
    assert resp.text.count("<item>") <= 5


async def test_search_offset_cannot_be_negative(client):
    c, _ = client
    resp = await c.get(f"/api?t=search&apikey={'k' * 32}&offset=-10")
    assert resp.status_code == 200


async def test_connection_test_rejects_crlf_in_credentials(client):
    """BLOCKER 2: CRLF in username injected raw NNTP commands."""
    c, _ = client
    resp = await c.post(
        "/setup/test",
        data={
            "provider_host": "127.0.0.1",
            "provider_port": "1",
            "provider_ssl": "no",
            "username": "x\r\nAUTHINFO USER evil",
            "password": "y",
        },
    )
    body = resp.json()
    assert body["ok"] is False
    assert "line break" in body["message"].lower() or body["field"] == "auth"


async def test_connection_test_rejects_privileged_ports(client):
    """BLOCKER 2: /setup/test was an arbitrary-port LAN scanner."""
    c, _ = client
    resp = await c.post(
        "/setup/test",
        data={
            "provider_host": "192.168.1.1",
            "provider_port": "22",
            "provider_ssl": "no",
            "username": "",
            "password": "",
        },
    )
    assert resp.json()["ok"] is False
    assert "port" in resp.json()["message"].lower()


def test_website_scheme_is_allowlisted():
    """BLOCKER 3: a spot's <Website> was rendered raw into href."""
    from app.spotnet import safe_website

    assert safe_website("https://example.com/x") == "https://example.com/x"
    assert safe_website("http://example.com") == "http://example.com"
    assert safe_website("javascript:alert(1)") == ""
    assert safe_website("JavaScript:alert(1)") == ""
    assert safe_website("  javascript:alert(1)") == ""
    assert safe_website("data:text/html;base64,x") == ""
    assert safe_website("vbscript:x") == ""
    assert safe_website("") == ""


async def test_password_change_requires_the_current_password(client):
    """BLOCKER 3 chain: takeover needed only one POST."""
    from app.auth import ADMIN, create_user, verify_credentials

    c, factory = client
    async with factory() as s:
        await create_user(s, "adminvv", "original", ADMIN)

    await c.post("/login", data={"username": "adminvv", "password": "original"})
    await c.post("/settings/account", data={"password": "hijacked"})

    async with factory() as s:
        assert await verify_credentials(s, "adminvv", "original") is not None
        assert await verify_credentials(s, "adminvv", "hijacked") is None


async def test_password_change_with_current_password_works_and_logs_out(client):
    from app.auth import ADMIN, create_user, verify_credentials
    from app.models import Session as SessionRow

    c, factory = client
    async with factory() as s:
        await create_user(s, "adminvv", "original", ADMIN)
    await c.post("/login", data={"username": "adminvv", "password": "original"})

    await c.post(
        "/settings/account",
        data={"current_password": "original", "password": "brand-new"},
    )
    async with factory() as s:
        assert await verify_credentials(s, "adminvv", "brand-new") is not None
        # every session is invalidated on a password change
        assert (await s.execute(select(SessionRow))).scalars().all() == []


def test_open_prefixes_match_whole_path_segments():
    """MINOR 11: startswith('/api') also opened a future /apikeys."""
    from app.main import is_open_path

    assert is_open_path("/api") is True
    assert is_open_path("/api?t=caps") is True
    assert is_open_path("/static/css/x.css") is True
    assert is_open_path("/apikeys") is False
    assert is_open_path("/api-admin") is False
    assert is_open_path("/settings") is False


# --- CORRECTNESS ------------------------------------------------------


def test_parser_never_raises_on_hostile_headers():
    """P0-1: int('²') raised, froze retrieval permanently."""
    from app.spotnet import parse_xover_row

    base = {
        "Number": 1,
        "Subject": "T | g",
        "Date": "Sat, 25 Jul 2026 20:00:00 +0200",
        "Message-ID": "x@y",
    }
    hostile = [
        "N <k@²³abc.100.r.d.n.n.S>",  # superscript "digits"
        "N <k@14a09." + "9" * 5000 + ".r.d.n.n.S>",  # filesize past int()'s digit limit
        "N <k@14a" + "9" * 6000 + ".100.r.d.n.n.S>",  # subcat run past the limit
        "N <k@١٤a09.100.r.d.n.n.S>",  # Arabic-Indic digits
        "N <k@.....>",
        "N <k@>",
    ]
    for frm in hostile:
        # The invariant is "never raises" — skipping the row or parsing it with
        # the bad parts dropped are both fine; an exception is not, because it
        # escapes the retrieve loop before the commit and freezes ingestion.
        result = parse_xover_row({**base, "From": frm})
        assert result is None or result.message_id == "<x@y>"


def test_stamp_without_timezone_is_treated_as_utc():
    """P2-11: '-0000' parsed naive, shifting every date by the host offset."""
    from app.spotnet import parse_xover_row

    spot = parse_xover_row(
        {
            "Number": 1,
            "Subject": "T | g",
            "From": "N <k@14a09b03c02d05.100.r.d.n.n.S>",
            "Date": "Sat, 25 Jul 2026 12:00:00 -0000",
            "Message-ID": "x@y",
        }
    )
    assert spot is not None
    assert spot.stamp == datetime(2026, 7, 25, 12, 0, tzinfo=UTC)


def test_subcat_truncation_never_splits_a_code():
    """P2-10: slicing mid-code produced fragments like 'd3' from 'd33'."""
    from app.retriever import pack_subcats

    codes = [f"d{n}" for n in range(1, 60)]
    packed = pack_subcats(codes, 128)
    assert len(packed) <= 128
    assert packed.endswith("|")
    for part in packed.split("|"):
        if part:
            assert part in codes


async def test_hide_adult_applies_on_the_unfiltered_landing_page(client):
    """P1-6: the empty tree short-circuited, so '/' still showed adult spots."""
    from app.settings_store import save_settings

    c, factory = client
    async with factory() as s:
        s.add(
            Spot(
                message_id="<ad@x>",
                title="Adult spot",
                posted_at=NOW,
                category=6040,
                spot_category=0,
                subcat_z="z3|",
            )
        )
        await s.commit()
        await save_settings(s, {"hide_adult": "on", "hide_adult__present": "1"})

    resp = await c.get("/")
    assert "Adult spot" not in resp.text
