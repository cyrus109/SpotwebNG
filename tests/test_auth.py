"""Tests for single-user authentication."""

import httpx
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.auth import ADMIN, account_exists, create_user, verify_credentials
from app.db import get_session
from app.main import app
from app.models import ApiUser, Base, Setting


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
        s.add(ApiUser(name="default", api_key="testkey"))
        s.add(Setting(key="provider_host", value="news.example.com"))
        await s.commit()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c, factory
    app.dependency_overrides.clear()
    await engine.dispose()


async def test_password_hash_is_not_reversible(client):
    _, factory = client
    async with factory() as s:
        user = await create_user(s, "adminvv", "hunter2", ADMIN)
        stored = user.password_hash
    assert "hunter2" not in stored
    assert len(stored) > 40  # salt + derived key


async def test_verify_password_accepts_only_the_real_password(client):
    _, factory = client
    async with factory() as s:
        await create_user(s, "adminvv", "hunter2", ADMIN)
        assert await verify_credentials(s, "adminvv", "hunter2") is not None
        assert await verify_credentials(s, "adminvv", "wrong") is None
        assert await verify_credentials(s, "someone", "hunter2") is None


async def test_no_account_means_open_access(client):
    """Before an account exists the app must stay usable (first-run)."""
    c, factory = client
    async with factory() as s:
        assert await account_exists(s) is False
    resp = await c.get("/")
    assert resp.status_code == 200


async def test_browsing_requires_login_once_an_account_exists(client):
    c, factory = client
    async with factory() as s:
        await create_user(s, "adminvv", "hunter2", ADMIN)

    resp = await c.get("/", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/login")


async def test_login_sets_a_session_and_grants_access(client):
    c, factory = client
    async with factory() as s:
        await create_user(s, "adminvv", "hunter2", ADMIN)

    bad = await c.post(
        "/login",
        data={"username": "adminvv", "password": "nope"},
        follow_redirects=False,
    )
    assert bad.status_code == 200 and "Incorrect" in bad.text

    ok = await c.post(
        "/login",
        data={"username": "adminvv", "password": "hunter2"},
        follow_redirects=False,
    )
    assert ok.status_code == 303
    assert "sw_session" in ok.cookies or any(
        "sw_session" in v for v in ok.headers.get_list("set-cookie")
    )

    page = await c.get("/", follow_redirects=False)
    assert page.status_code == 200


async def test_logout_revokes_the_session(client):
    c, factory = client
    async with factory() as s:
        await create_user(s, "adminvv", "hunter2", ADMIN)
    await c.post("/login", data={"username": "adminvv", "password": "hunter2"})
    await c.post("/logout")
    resp = await c.get("/", follow_redirects=False)
    assert resp.status_code == 303


async def test_newznab_api_stays_reachable_without_a_session(client):
    """Radarr/Sonarr authenticate with the API key, not a cookie."""
    c, factory = client
    async with factory() as s:
        await create_user(s, "adminvv", "hunter2", ADMIN)

    caps = await c.get("/api?t=caps")
    assert caps.status_code == 200 and "<caps>" in caps.text

    search = await c.get("/api?t=search&q=x&apikey=testkey")
    assert search.status_code == 200 and "<error" not in search.text

    healthz = await c.get("/healthz")
    assert healthz.status_code == 200


# ---------- first-run: the wizard must create the account ----------


async def test_wizard_creates_the_account_on_first_run(client):
    """A fresh install that only configures a provider stays open forever."""
    from app.auth import account_exists, verify_credentials

    c, factory = client
    resp = await c.post(
        "/setup",
        data={
            "provider_host": "news.example.com",
            "provider_port": "563",
            "provider_ssl": "yes",
            "username": "alice",
            "password": "usenet-pw",
            "connections": "4",
            "account_username": "adminvv",
            "account_password": "my-login-pw",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    async with factory() as s:
        assert await account_exists(s) is not None
        assert await verify_credentials(s, "adminvv", "my-login-pw") is not None
        # the Usenet password must not be reused as the login password
        assert await verify_credentials(s, "adminvv", "usenet-pw") is None


async def test_wizard_does_not_clobber_an_existing_account(client):
    from app.auth import ADMIN, create_user, verify_credentials

    c, factory = client
    async with factory() as s:
        await create_user(s, "adminvv", "original", ADMIN)
    await c.post("/login", data={"username": "adminvv", "password": "original"})

    await c.post(
        "/setup",
        data={
            "provider_host": "news.example.com",
            "provider_port": "563",
            "provider_ssl": "yes",
            "username": "alice",
            "password": "pw",
            "connections": "4",
            "account_username": "someone-else",
            "account_password": "hijack",
        },
    )
    async with factory() as s:
        assert await verify_credentials(s, "adminvv", "original") is not None


async def test_setup_page_offers_account_fields_only_when_none_exists(client):
    c, factory = client
    first = await c.get("/setup")
    assert "account_password" in first.text

    async with factory() as s:
        await create_user(s, "adminvv", "pw", ADMIN)
    await c.post("/login", data={"username": "adminvv", "password": "pw"})
    second = await c.get("/setup")
    assert "account_password" not in second.text


# ---------- login rate limiting ----------


async def test_repeated_bad_logins_are_throttled(client):
    from app.auth import ADMIN, create_user, reset_login_throttle

    c, factory = client
    reset_login_throttle()
    async with factory() as s:
        await create_user(s, "adminvv", "correct-horse", ADMIN)

    for _ in range(5):
        await c.post("/login", data={"username": "adminvv", "password": "wrong"})

    blocked = await c.post("/login", data={"username": "adminvv", "password": "correct-horse"})
    assert "too many" in blocked.text.lower()

    reset_login_throttle()
    ok = await c.post(
        "/login",
        data={"username": "adminvv", "password": "correct-horse"},
        follow_redirects=False,
    )
    assert ok.status_code == 303
