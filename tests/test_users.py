"""Multi-user accounts with roles: admins configure, viewers only browse."""

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.auth import (
    ADMIN,
    VIEWER,
    create_user,
    reset_login_throttle,
    user_for_session,
    verify_credentials,
)
from app.db import get_session
from app.main import app
from app.models import ApiUser, Base, Setting, User


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
    reset_login_throttle()
    async with factory() as s:
        s.add(ApiUser(name="default", api_key="k" * 32))
        s.add(Setting(key="provider_host", value="news.example.com"))
        await s.commit()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c, factory
    app.dependency_overrides.clear()
    await engine.dispose()


async def _login(c, username, password):
    return await c.post(
        "/login",
        data={"username": username, "password": password},
        follow_redirects=False,
    )


# --- model ------------------------------------------------------------


async def test_users_are_stored_with_a_role_and_hashed_password(client):
    _, factory = client
    async with factory() as s:
        await create_user(s, "admin1", "pw-admin-123", ADMIN)
        row = (await s.execute(select(User))).scalars().one()
    assert row.username == "admin1"
    assert row.role == ADMIN
    assert "pw-admin-123" not in row.password_hash


async def test_usernames_are_unique_and_case_insensitive(client):
    _, factory = client
    async with factory() as s:
        await create_user(s, "Alice", "pw-alice-123", ADMIN)
        with pytest.raises(ValueError):
            await create_user(s, "alice", "other-pw-123", VIEWER)


async def test_verify_credentials_returns_the_user_or_none(client):
    _, factory = client
    async with factory() as s:
        await create_user(s, "alice", "pw-alice-123", VIEWER)
        assert (await verify_credentials(s, "alice", "pw-alice-123")).role == VIEWER
        assert await verify_credentials(s, "alice", "wrong") is None
        assert await verify_credentials(s, "nobody", "pw-alice-123") is None


async def test_session_is_bound_to_its_user(client):
    from app.auth import start_session

    _, factory = client
    async with factory() as s:
        user = await create_user(s, "alice", "pw-alice-123", VIEWER)
        token = await start_session(s, user)
        assert (await user_for_session(s, token)).username == "alice"
        assert await user_for_session(s, "not-a-token") is None


# --- role enforcement -------------------------------------------------


async def test_viewer_can_browse_but_not_reach_settings(client):
    c, factory = client
    async with factory() as s:
        await create_user(s, "admin1", "pw-admin-123", ADMIN)
        await create_user(s, "vic", "pw-viewer-123", VIEWER)

    await _login(c, "vic", "pw-viewer-123")
    assert (await c.get("/", follow_redirects=False)).status_code == 200

    for path in ("/settings", "/setup"):
        blocked = await c.get(path, follow_redirects=False)
        assert blocked.status_code in (302, 303, 403), path


async def test_admin_reaches_settings(client):
    c, factory = client
    async with factory() as s:
        await create_user(s, "admin1", "pw-admin-123", ADMIN)
    await _login(c, "admin1", "pw-admin-123")
    assert (await c.get("/settings", follow_redirects=False)).status_code == 200


async def test_viewer_cannot_change_settings_or_users(client):
    c, factory = client
    async with factory() as s:
        await create_user(s, "admin1", "pw-admin-123", ADMIN)
        await create_user(s, "vic", "pw-viewer-123", VIEWER)
    await _login(c, "vic", "pw-viewer-123")

    resp = await c.post("/settings", data={"per_page": "13"}, follow_redirects=False)
    assert resp.status_code in (302, 303, 403)

    created = await c.post(
        "/settings/users",
        data={"username": "sneak", "password": "pw-sneak-123", "role": ADMIN},
        follow_redirects=False,
    )
    assert created.status_code in (302, 303, 403)
    async with factory() as s:
        assert (
            await s.execute(select(User).where(User.username == "sneak"))
        ).scalar_one_or_none() is None


# --- management -------------------------------------------------------


async def test_admin_can_add_and_remove_users(client):
    c, factory = client
    async with factory() as s:
        await create_user(s, "admin1", "pw-admin-123", ADMIN)
    await _login(c, "admin1", "pw-admin-123")

    await c.post(
        "/settings/users",
        data={"username": "vic", "password": "pw-viewer-123", "role": VIEWER},
    )
    async with factory() as s:
        vic = (await s.execute(select(User).where(User.username == "vic"))).scalar_one()
        assert vic.role == VIEWER

    await c.post("/settings/users/delete", data={"user_id": vic.id})
    async with factory() as s:
        assert (
            await s.execute(select(User).where(User.username == "vic"))
        ).scalar_one_or_none() is None


async def test_the_last_admin_cannot_be_removed(client):
    """Deleting the only admin would lock everyone out of Settings forever."""
    c, factory = client
    async with factory() as s:
        admin = await create_user(s, "admin1", "pw-admin-123", ADMIN)
        await create_user(s, "vic", "pw-viewer-123", VIEWER)
    await _login(c, "admin1", "pw-admin-123")

    await c.post("/settings/users/delete", data={"user_id": admin.id})
    async with factory() as s:
        assert (
            await s.execute(select(User).where(User.username == "admin1"))
        ).scalar_one_or_none() is not None


async def test_the_last_admin_cannot_be_demoted(client):
    c, factory = client
    async with factory() as s:
        admin = await create_user(s, "admin1", "pw-admin-123", ADMIN)
    await _login(c, "admin1", "pw-admin-123")

    await c.post("/settings/users/role", data={"user_id": admin.id, "role": VIEWER})
    async with factory() as s:
        assert (await s.get(User, admin.id)).role == ADMIN


# --- upgrade path -----------------------------------------------------


async def test_a_legacy_single_account_becomes_an_admin_user(client):
    """Installs created before roles kept the login in `settings` rows."""
    import secrets

    from app.auth import _hash, migrate_legacy_account

    _, factory = client
    async with factory() as s:
        s.add(Setting(key="auth_username", value="olduser"))
        s.add(
            Setting(
                key="auth_password",
                value=_hash("legacy-pw-123", secrets.token_bytes(16)),
            )
        )
        await s.commit()
        await migrate_legacy_account(s)

        user = (await s.execute(select(User))).scalars().one()
        assert user.username == "olduser" and user.role == ADMIN
        assert (await verify_credentials(s, "olduser", "legacy-pw-123")) is not None
        # the legacy rows are cleared so there is one source of truth
        assert await s.get(Setting, "auth_password") is None


# --- adult content is an admin-only, safe-by-default control ---------


async def test_hiding_adult_content_is_on_by_default(client):
    """A fresh install must not show adult covers before anyone configures it."""
    from app.settings_store import load_settings

    _, factory = client
    async with factory() as s:
        assert (await load_settings(s))["hide_adult"] is True


async def test_only_an_admin_can_reveal_adult_content(client):
    from app.settings_store import load_settings

    c, factory = client
    async with factory() as s:
        await create_user(s, "admin1", "pw-admin-123", ADMIN)
        await create_user(s, "vic", "pw-viewer-123", VIEWER)

    # a viewer cannot turn the filter off
    await _login(c, "vic", "pw-viewer-123")
    await c.post("/settings", data={"hide_adult__present": "1"})
    async with factory() as s:
        assert (await load_settings(s))["hide_adult"] is True

    # an admin can
    await c.post("/logout")
    await _login(c, "admin1", "pw-admin-123")
    await c.post("/settings", data={"hide_adult__present": "1"})
    async with factory() as s:
        assert (await load_settings(s))["hide_adult"] is False
