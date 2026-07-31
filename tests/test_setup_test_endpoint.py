"""Tests for POST /setup/test — the wizard's connection check endpoint."""

import asyncio

import httpx
import pytest

from app.main import app
from tests.test_nntp_check import GOOD_PASS, GOOD_USER, _fake_nntp


@pytest.fixture
async def client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
async def nntp_server():
    server = await asyncio.start_server(_fake_nntp, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    yield port
    server.close()
    await server.wait_closed()


async def test_endpoint_reports_success(client, nntp_server):
    resp = await client.post(
        "/setup/test",
        data={
            "provider_host": "127.0.0.1",
            "provider_port": nntp_server,
            "provider_ssl": "no",
            "username": GOOD_USER,
            "password": GOOD_PASS,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True


async def test_endpoint_reports_auth_failure(client, nntp_server):
    resp = await client.post(
        "/setup/test",
        data={
            "provider_host": "127.0.0.1",
            "provider_port": nntp_server,
            "provider_ssl": "no",
            "username": GOOD_USER,
            "password": "wrong",
        },
    )
    body = resp.json()
    assert body["ok"] is False
    assert body["field"] == "auth"


async def test_endpoint_reports_unreachable_host(client):
    resp = await client.post(
        "/setup/test",
        data={
            "provider_host": "127.0.0.1",
            "provider_port": 1,
            "provider_ssl": "no",
            "username": "",
            "password": "",
        },
    )
    body = resp.json()
    assert body["ok"] is False
    assert body["field"] == "host"
