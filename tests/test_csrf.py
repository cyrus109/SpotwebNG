"""CSRF: unsafe requests made with a session cookie must carry that session's token."""

import httpx
import pytest

from app.auth import ADMIN, create_user
from app.csrf import token_for
from app.db import get_session
from app.main import app

pytestmark = pytest.mark.raw_csrf


@pytest.fixture
async def web():
    factory = None
    # conftest installed an in-memory session override; reuse its factory
    override = app.dependency_overrides[get_session]
    async for session in override():
        factory = session.bind
        break
    from sqlalchemy.ext.asyncio import async_sessionmaker

    maker = async_sessionmaker(factory, expire_on_commit=False)
    async with maker() as s:
        await create_user(s, "boss", "pw-boss-12345", ADMIN)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _login(c) -> str:
    resp = await c.post("/login", data={"username": "boss", "password": "pw-boss-12345"})
    assert resp.status_code == 303
    return c.cookies["sw_session"]


async def test_a_signed_in_post_without_the_token_is_refused(web):
    await _login(web)
    for kwargs in ({}, {"headers": {"X-CSRF-Token": "wrong"}}, {"data": {"csrf_token": "wrong"}}):
        resp = await web.post("/settings/apikey", **({"data": {"name": "x"}} | kwargs))
        assert resp.status_code == 403, kwargs


async def test_the_right_token_works_as_header_and_as_form_field(web):
    cookie = await _login(web)
    token = token_for(cookie)
    ok_header = await web.post(
        "/settings/apikey", data={"name": "a"}, headers={"X-CSRF-Token": token}
    )
    ok_field = await web.post("/settings/apikey", data={"name": "b", "csrf_token": token})
    assert ok_header.status_code == 303 and ok_field.status_code == 303


async def test_another_sessions_token_is_refused(web):
    await _login(web)
    other = token_for("some-other-session-cookie")
    resp = await web.post("/settings/apikey", data={"name": "x", "csrf_token": other})
    assert resp.status_code == 403


async def test_pages_carry_the_token_and_json_endpoints_check_it_too(web):
    cookie = await _login(web)
    page = (await web.get("/settings")).text
    assert f'name="csrf-token" content="{token_for(cookie)}"' in page
    assert (await web.post("/retrieve")).status_code == 403  # fetch() without the header
    assert (
        await web.post("/retrieve", headers={"X-CSRF-Token": token_for(cookie)})
    ).status_code != 403


async def test_signed_out_requests_are_not_blocked_so_signing_in_works(web):
    page = (await web.get("/login")).text
    assert 'name="csrf-token" content=""' in page
    assert (await web.post("/login", data={"username": "boss", "password": "x"})).status_code == 200


async def test_a_stale_cookie_can_still_sign_in_with_the_token_its_page_carried(web):
    web.cookies.set("sw_session", "expired-cookie")
    page = (await web.get("/login")).text
    assert token_for("expired-cookie") in page
    resp = await web.post(
        "/login",
        data={
            "username": "boss",
            "password": "pw-boss-12345",
            "csrf_token": token_for("expired-cookie"),
        },
    )
    assert resp.status_code == 303


async def test_the_newznab_api_is_unaffected(web):
    cookie = await _login(web)
    assert cookie
    assert (await web.get("/api?t=caps")).status_code == 200
