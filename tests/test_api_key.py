"""Tests for API key generation/rotation from the setup wizard."""

import re

import httpx
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import get_session
from app.main import app
from app.models import ApiUser, Base
from app.seed import seed_if_empty

# No key ships with the app any more, so the tests pin their own.
DEFAULT_KEY = "test-default-key-0001"


@pytest.fixture
async def client():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async def override():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override
    async with session_factory() as session:
        session.add(ApiUser(name="default", api_key=DEFAULT_KEY))
        await session.commit()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c, session_factory
    app.dependency_overrides.clear()
    await engine.dispose()


async def test_generate_returns_fresh_hex_key(client):
    c, _ = client
    resp = await c.post("/setup/apikey")
    assert resp.status_code == 200
    key = resp.json()["key"]
    assert re.fullmatch(r"[0-9a-f]{32}", key)
    assert key != DEFAULT_KEY


async def test_new_key_works_and_old_key_is_revoked(client):
    c, _ = client
    key = (await c.post("/setup/apikey")).json()["key"]

    ok = await c.get(f"/api?t=search&q=x&apikey={key}")
    assert "<error" not in ok.text

    revoked = await c.get(f"/api?t=search&q=x&apikey={DEFAULT_KEY}")
    assert 'code="100"' in revoked.text


async def test_seed_does_not_resurrect_default_key(client):
    c, session_factory = client
    await c.post("/setup/apikey")

    async with session_factory() as session:
        await seed_if_empty(session)

    revoked = await c.get(f"/api?t=search&q=x&apikey={DEFAULT_KEY}")
    assert 'code="100"' in revoked.text


async def test_api_use_records_last_used(client):
    """'Is this key still in use?' must be answerable before revoking it."""
    from sqlalchemy import select

    from app.models import ApiUser

    c, session_factory = client
    async with session_factory() as s:
        before = (await s.execute(select(ApiUser))).scalars().first()
        assert before.last_used is None

    await c.get(f"/api?t=search&q=x&apikey={DEFAULT_KEY}")

    async with session_factory() as s:
        after = (await s.execute(select(ApiUser))).scalars().first()
        assert after.last_used is not None


async def test_caps_does_not_count_as_use(client):
    """t=caps needs no key, so it must not stamp one."""
    from sqlalchemy import select

    from app.models import ApiUser

    c, session_factory = client
    await c.get("/api?t=caps")
    async with session_factory() as s:
        row = (await s.execute(select(ApiUser))).scalars().first()
        assert row.last_used is None
