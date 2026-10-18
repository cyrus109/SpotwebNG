"""End-to-end smoke tests for the web and API routes.

These exercise the wiring — templates render, redirects go where they should,
error paths return the right status — which unit tests of the services miss.
"""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.auth import ADMIN, create_user, reset_login_throttle
from app.db import get_session
from app.main import app
from app.models import ApiUser, Base, Setting, Spot

NOW = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
KEY = "k" * 32


@pytest.fixture
async def client(tmp_path):
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def override():
        async with factory() as s:
            yield s

    app.dependency_overrides[get_session] = override
    reset_login_throttle()
    async with factory() as s:
        s.add(ApiUser(name="default", api_key=KEY))
        for key, value in {
            "provider_host": "news.example.com",
            "provider_port": "563",
            "provider_ssl": "yes",
            "username": "alice",
            "password": "usenet-pw",
        }.items():
            s.add(Setting(key=key, value=value))
        for i in range(8):
            s.add(
                Spot(
                    message_id=f"<r{i}@x>",
                    title=f"Spot {i}",
                    posted_at=NOW - timedelta(hours=i),
                    category=2040 if i % 2 else 5040,
                    spot_category=0,
                    subcat_a="a9|",
                    subcat_z="z0|" if i % 2 else "z1|",
                    size=1_000_000_000,
                    enriched=1,
                )
            )
        await s.commit()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c, factory
    app.dependency_overrides.clear()
    await engine.dispose()


async def _admin(c, factory):
    async with factory() as s:
        await create_user(s, "boss", "pw-boss-12345", ADMIN)
    await c.post("/login", data={"username": "boss", "password": "pw-boss-12345"})


# --- browse -----------------------------------------------------------


@pytest.mark.parametrize(
    "query",
    [
        "",
        "?q=Spot",
        "?tree=cat0_z0",
        "?tree=cat0_z0,cat0_z0_a9",
        "?tree=cat0_z1&sort=old",
        "?sort=size",
        "?sort=title",
        "?age=24h",
        "?view=table",
        "?page=2",
        "?page=99999",
        "?minsize=1&maxsize=100",
    ],
)
async def test_browse_renders(client, query):
    c, _ = client
    resp = await c.get(f"/{query}")
    assert resp.status_code == 200
    assert "<html" in resp.text.lower()


async def test_browse_shows_the_advanced_filter_drawer(client):
    c, _ = client
    resp = await c.get("/?tree=cat0_z0")
    assert "Advanced filters" in resp.text
    assert "data-dim-tab" in resp.text


async def test_spot_detail_and_missing_spot(client):
    c, factory = client
    async with factory() as s:
        from sqlalchemy import select

        spot = (await s.execute(select(Spot))).scalars().first()
    ok = await c.get(f"/spot/{spot.id}")
    assert ok.status_code == 200 and spot.title in ok.text
    assert (await c.get("/spot/999999")).status_code == 404


async def test_missing_image_is_a_404(client):
    c, _ = client
    assert (await c.get("/image/999999")).status_code == 404


# --- settings ---------------------------------------------------------


async def test_settings_page_renders_every_section(client):
    c, factory = client
    await _admin(c, factory)
    resp = await c.get("/settings")
    assert resp.status_code == 200
    for section in ("usenet", "retrieval", "display", "appearance", "api", "users"):
        assert f'id="{section}"' in resp.text


async def test_saving_settings_round_trips(client):
    c, factory = client
    await _admin(c, factory)
    await c.post("/settings", data={"per_page": "42", "default_sort": "old"})
    from app.settings_store import load_settings

    async with factory() as s:
        values = await load_settings(s)
    assert values["per_page"] == 42
    assert values["default_sort"] == "old"


async def test_api_keys_can_be_created_and_revoked(client):
    from sqlalchemy import select

    c, factory = client
    await _admin(c, factory)
    await c.post("/settings/apikey", data={"name": "Radarr"})
    async with factory() as s:
        keys = (await s.execute(select(ApiUser))).scalars().all()
        radarr = next(k for k in keys if k.name == "Radarr")
    assert len(radarr.api_key) == 32

    await c.post("/settings/apikey/delete", data={"key_id": radarr.id})
    async with factory() as s:
        names = [k.name for k in (await s.execute(select(ApiUser))).scalars().all()]
    assert "Radarr" not in names


async def test_setup_wizard_renders(client):
    c, factory = client
    await _admin(c, factory)
    resp = await c.get("/setup")
    assert resp.status_code == 200 and "provider_host" in resp.text


async def test_retrieve_reports_a_failure_without_leaking_a_traceback(client):
    c, factory = client
    await _admin(c, factory)
    resp = await c.post("/retrieve")
    assert resp.status_code in (400, 502)
    body = resp.json()
    assert body["ok"] is False
    assert "Traceback" not in body["message"]


async def test_connection_test_from_settings_reports_cleanly(client):
    c, factory = client
    await _admin(c, factory)
    resp = await c.post("/settings/test")
    assert resp.status_code == 200
    assert resp.json()["ok"] is False  # example.com is not a news server


# --- newznab ----------------------------------------------------------


@pytest.mark.parametrize("t", ["caps", "search", "tvsearch", "movie", "music"])
async def test_api_functions_return_xml(client, t):
    c, _ = client
    resp = await c.get(f"/api?t={t}&apikey={KEY}")
    assert resp.status_code == 200
    assert resp.text.lstrip().startswith("<?xml")


async def test_api_rejects_unknown_function_and_bad_key(client):
    c, _ = client
    assert 'code="202"' in (await c.get(f"/api?t=nonsense&apikey={KEY}")).text
    assert 'code="100"' in (await c.get("/api?t=search&apikey=wrong")).text


async def test_api_get_requires_an_id_and_handles_a_missing_spot(client):
    c, _ = client
    assert 'code="200"' in (await c.get(f"/api?t=get&apikey={KEY}")).text
    assert 'code="300"' in (await c.get(f"/api?t=get&id=999999&apikey={KEY}")).text


async def test_api_get_returns_an_nzb_for_a_known_spot(client):
    from sqlalchemy import select

    c, factory = client
    async with factory() as s:
        spot = (await s.execute(select(Spot))).scalars().first()
    resp = await c.get(f"/api?t=get&id={spot.id}&apikey={KEY}")
    assert resp.status_code == 200
    assert "<nzb" in resp.text


async def test_api_search_filters_by_category(client):
    c, _ = client
    tv = await c.get(f"/api?t=search&cat=5000&apikey={KEY}")
    assert tv.text.count("<item>") == 4  # the z1 half of the fixture


async def test_api_get_serves_a_cached_nzb_without_touching_usenet(client, monkeypatch, tmp_path):
    """Once assembled, an NZB is served from disk — no provider round-trip."""
    import json

    from sqlalchemy import select

    from app import newznab

    monkeypatch.setattr(newznab, "NZB_DIR", tmp_path)

    c, factory = client
    async with factory() as s:
        spot = (await s.execute(select(Spot))).scalars().first()
        spot.nzb_segments = json.dumps(["seg-1@spot.net"])
        await s.commit()
        spot_id = spot.id

    (tmp_path / f"{spot_id}.nzb").write_bytes(b'<?xml version="1.0"?><nzb>cached</nzb>')

    resp = await c.get(f"/api?t=get&id={spot_id}&apikey={KEY}")
    assert resp.status_code == 200
    assert b"cached" in resp.content
    assert resp.headers["content-disposition"].endswith(f'{spot_id}.nzb"')


async def test_detail_page_of_an_unenriched_spot_still_renders(client):
    """Enrichment on first view must not break the page when it cannot run."""
    from sqlalchemy import select

    c, factory = client
    async with factory() as s:
        spot = (await s.execute(select(Spot))).scalars().first()
        spot.enriched = 0
        await s.commit()
        spot_id = spot.id

    resp = await c.get(f"/spot/{spot_id}")
    assert resp.status_code == 200


async def test_logout_redirects_to_login(client):
    c, factory = client
    await _admin(c, factory)
    resp = await c.post("/logout", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


async def test_login_page_renders_when_an_account_exists(client):
    c, factory = client
    async with factory() as s:
        await create_user(s, "boss", "pw-boss-12345", ADMIN)
    resp = await c.get("/login")
    assert resp.status_code == 200 and "Sign in" in resp.text


async def test_admin_can_reset_another_users_password(client):
    """Resetting a password must also end that user's sessions."""
    from sqlalchemy import select

    from app.auth import VIEWER, verify_credentials
    from app.models import Session as SessionRow

    c, factory = client
    await _admin(c, factory)
    await c.post(
        "/settings/users",
        data={"username": "vic", "password": "old-password-1", "role": VIEWER},
    )
    async with factory() as s:
        from app.models import User

        vic = (await s.execute(select(User).where(User.username == "vic"))).scalar_one()

    await c.post(
        "/settings/users/password",
        data={"user_id": vic.id, "password": "new-password-1"},
    )
    async with factory() as s:
        assert await verify_credentials(s, "vic", "new-password-1") is not None
        remaining = (
            (await s.execute(select(SessionRow).where(SessionRow.user_id == vic.id)))
            .scalars()
            .all()
        )
    assert remaining == []


async def test_stored_cover_art_is_served(client, tmp_path, monkeypatch):
    from sqlalchemy import select

    from app import web

    monkeypatch.setattr(web, "IMAGES_DIR", tmp_path)
    c, factory = client
    async with factory() as s:
        spot = (await s.execute(select(Spot))).scalars().first()
        spot_id = spot.id
    (tmp_path / f"{spot_id}.jpg").write_bytes(b"\xff\xd8\xff\xe0fake-jpeg")

    resp = await c.get(f"/image/{spot_id}")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/jpeg"


# --- client compatibility --------------------------------------------


async def test_caps_advertises_every_search_mode_we_support(client):
    """Lidarr and Readarr read caps and refuse an indexer that omits their mode."""
    import xml.etree.ElementTree as ET

    c, _ = client
    root = ET.fromstring((await c.get("/api?t=caps")).text)
    searching = {child.tag: child.get("available") for child in root.find("searching")}
    for mode in ("search", "tv-search", "movie-search", "audio-search", "book-search"):
        assert searching.get(mode) == "yes", mode


@pytest.mark.parametrize(
    "query",
    [
        "t=music&artist=Miles&album=Kind+of+Blue",  # Lidarr
        "t=book&author=Tolkien&title=Hobbit",  # Readarr
        "t=tvsearch&q=Show&season=2&ep=5",  # Sonarr
        "t=movie&imdbid=0111161",  # Radarr
    ],
)
async def test_client_search_shapes_are_accepted(client, query):
    """Each *arr sends its own parameters; none may produce an error document."""
    c, _ = client
    resp = await c.get(f"/api?{query}&apikey={KEY}")
    assert resp.status_code == 200
    assert "<error" not in resp.text


# --- the adult filter must hold on every surface -----------------------


@pytest.fixture
async def with_adult(client):
    """A client whose index contains one erotica spot, with hiding enabled."""
    from app.models import Setting as SettingRow

    c, factory = client
    async with factory() as s:
        s.add(
            Spot(
                message_id="<xxx@x>",
                title="Definitely Not Safe For Work",
                posted_at=NOW,
                category=6040,
                spot_category=0,
                subcat_z="z3|",
                size=1,
                enriched=1,
            )
        )
        # 18 spots in the real index carry z3 but sit outside the 6xxx range,
        # so category alone is not a sufficient test.
        s.add(
            Spot(
                message_id="<xxx2@x>",
                title="Also Not Safe, Filed Under Movies",
                posted_at=NOW,
                category=2040,
                spot_category=0,
                subcat_z="z3|",
                size=1,
                enriched=1,
            )
        )
        s.add(SettingRow(key="hide_adult", value="yes"))
        await s.commit()
    return c, factory


async def _adult_ids(factory):
    from sqlalchemy import select

    async with factory() as s:
        rows = (await s.execute(select(Spot).where(Spot.subcat_z.like("%z3|%")))).scalars().all()
        return [r.id for r in rows]


async def test_api_search_hides_adult_when_the_admin_hid_it(with_adult):
    """An RSS feed is the easiest way for hidden content to reach a screen."""
    c, _ = with_adult
    body = (await c.get(f"/api?t=search&apikey={KEY}")).text
    assert "Not Safe" not in body


async def test_api_cannot_be_asked_for_the_adult_category_directly(with_adult):
    c, _ = with_adult
    body = (await c.get(f"/api?t=search&cat=6000&apikey={KEY}")).text
    assert "Not Safe" not in body


async def test_caps_omits_the_adult_category_when_hidden(with_adult):
    """A client must not offer a category the instance refuses to serve."""
    c, _ = with_adult
    assert 'id="6000"' not in (await c.get("/api?t=caps")).text


async def test_detail_page_of_a_hidden_adult_spot_is_a_404(with_adult):
    c, factory = with_adult
    for spot_id in await _adult_ids(factory):
        assert (await c.get(f"/spot/{spot_id}")).status_code == 404


async def test_cover_art_of_a_hidden_adult_spot_is_not_served(with_adult, tmp_path, monkeypatch):
    """This is the exact failure that put adult images on a public page."""
    from app import web

    monkeypatch.setattr(web, "IMAGES_DIR", tmp_path)
    c, factory = with_adult
    for spot_id in await _adult_ids(factory):
        (tmp_path / f"{spot_id}.jpg").write_bytes(b"\xff\xd8\xff\xe0porn")
        assert (await c.get(f"/image/{spot_id}")).status_code == 404


async def test_adult_content_is_served_when_the_admin_allows_it(with_adult):
    """The filter is a setting, not a ban — an admin can still turn it off."""
    from sqlalchemy import select

    from app.models import Setting as SettingRow

    c, factory = with_adult
    async with factory() as s:
        row = (
            await s.execute(select(SettingRow).where(SettingRow.key == "hide_adult"))
        ).scalar_one()
        row.value = "no"
        await s.commit()
    body = (await c.get(f"/api?t=search&apikey={KEY}")).text
    assert "Not Safe" in body


# --- comments ----------------------------------------------------------


async def test_detail_page_shows_comments_escaped_and_the_grid_shows_a_count(client):
    from sqlalchemy import select

    from app.models import Comment

    c, factory = client
    async with factory() as s:
        spot = (await s.execute(select(Spot).where(Spot.message_id == "<r1@x>"))).scalar_one()
        s.add(
            Comment(
                message_id="<r1.5.a.b@x>",
                spot_message_id="<r1@x>",
                poster="<b>Mallory</b>",
                posted_at=NOW,
                body="<script>alert(1)</script> hi",
                body_state=1,
            )
        )
        await s.commit()
        spot_id = spot.id
    page = (await c.get(f"/spot/{spot_id}")).text
    assert "Comments (1)" in page
    assert "<script>alert(1)</script>" not in page and "&lt;script&gt;" in page
    assert "&lt;b&gt;Mallory" in page
    assert "💬 1" in (await c.get("/")).text


async def test_comments_of_a_hidden_adult_spot_are_unreachable(with_adult):
    from sqlalchemy import select

    c, factory = with_adult
    async with factory() as s:
        adult = (await s.execute(select(Spot).where(Spot.message_id == "<xxx@x>"))).scalar_one()
    assert (await c.get(f"/spot/{adult.id}")).status_code == 404


# --- status and backup -------------------------------------------------


async def test_status_and_export_are_admin_only(client):
    c, factory = client
    async with factory() as s:
        await create_user(s, "boss", "pw-boss-12345", ADMIN)
        await create_user(s, "view", "pw-view-12345", "viewer")
    await c.post("/login", data={"username": "view", "password": "pw-view-12345"})
    for path in ("/retrieve/status", "/settings/export"):
        assert (await c.get(path)).status_code in (303, 307)  # sent away, no data
    await c.post("/logout")
    await c.post("/login", data={"username": "boss", "password": "pw-boss-12345"})
    body = (await c.get("/retrieve/status")).json()
    assert body["live"]["running"] is False and "status" in body
    export = await c.get("/settings/export")
    assert export.status_code == 200
    assert "attachment" in export.headers["content-disposition"]
    import gzip

    text = gzip.decompress(export.content).decode()
    assert '"spotwebng-export"' in text and "usenet-pw" not in text
    assert (await c.get("/settings")).text.count("Status &amp; backup") >= 1


async def test_import_route_runs_in_the_background_and_reports(client):
    import asyncio

    c, factory = client
    await _admin(c, factory)
    exported = (await c.get("/settings/export")).content
    resp = await c.post(
        "/settings/import", files={"file": ("backup.jsonl.gz", exported, "application/gzip")}
    )
    assert resp.json() == {"ok": True, "started": True}
    for _ in range(100):
        status = (await c.get("/retrieve/status")).json()["status"]
        if "last_import" in status:
            break
        await asyncio.sleep(0.05)
    assert status["last_import"]["ok"] is True
    assert status["last_import"]["added"] == {}  # everything was already here
    bad = await c.post("/settings/import", files={"file": ("x.gz", b"junk", "application/gzip")})
    assert bad.json()["ok"] is True  # accepted, then reported as failed
    for _ in range(100):
        status = (await c.get("/retrieve/status")).json()["status"]
        if status["last_import"].get("ok") is False:
            break
        await asyncio.sleep(0.05)
    assert status["last_import"]["ok"] is False
