"""Shared test setup.

Every test gets its own in-memory database. Without this a test that builds an
httpx client against `app` without overriding the session dependency silently
uses the developer's real SQLite file — which passes locally and fails on CI
with "no such table", because there the file does not exist.
"""

import asyncio

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import get_session
from app.main import app
from app.models import Base


@pytest.fixture(autouse=True)
async def isolated_database():
    """Point the app at a throwaway database unless the test overrides it."""
    if get_session in app.dependency_overrides:
        yield  # the test installed its own; leave it alone
        return

    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def override():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_session] = override
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_session, None)
        await engine.dispose()


@pytest.fixture(autouse=True)
def reset_throttle():
    """Login throttling is process-global; one test must not lock out the next."""
    from app.auth import reset_login_throttle

    reset_login_throttle()
    yield
    reset_login_throttle()


@pytest.fixture(autouse=True)
def no_slow_dns(monkeypatch):
    """Resolving a deliberately invalid hostname can stall for seconds on a CI
    runner. Fail it immediately instead, which is what the tests assert."""
    real_open = asyncio.open_connection

    async def guarded(host=None, port=None, *args, **kwargs):
        if isinstance(host, str) and host.endswith(".invalid"):
            raise OSError("Name or service not known")
        return await real_open(host, port, *args, **kwargs)

    monkeypatch.setattr(asyncio, "open_connection", guarded)


@pytest.fixture(autouse=True)
def csrf_token_for_test_clients(request, monkeypatch):
    """Logged-in test clients behave like the browser: send the CSRF header. Mark a
    test `raw_csrf` to send requests exactly as built."""
    if request.node.get_closest_marker("raw_csrf"):
        return
    import re

    import httpx

    from app.csrf import token_for

    real = httpx.AsyncClient.build_request

    def build(self, *args, **kwargs):
        built = real(self, *args, **kwargs)
        cookies = built.headers.get("cookie", "")
        found = re.search(r"sw_session=([^;]+)", cookies) or re.search(r"sw_csrf=([^;]+)", cookies)
        if found is None:  # a browser would have been handed the pre-session cookie
            built.headers["cookie"] = (cookies + "; sw_csrf=test-csrf").lstrip("; ")
            secret = "test-csrf"
        else:
            secret = found.group(1)
        if "x-csrf-token" not in built.headers:
            built.headers["X-CSRF-Token"] = token_for(secret)
        return built

    monkeypatch.setattr(httpx.AsyncClient, "build_request", build)
