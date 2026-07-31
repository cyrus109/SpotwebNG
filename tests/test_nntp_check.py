"""Tests for the NNTP connection check used by the setup wizard."""

import asyncio

import pytest

from app.nntp_check import check_nntp

GOOD_USER = "alice"
GOOD_PASS = "s3cret"


async def _fake_nntp(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    writer.write(b"200 fake.news.example ready\r\n")
    await writer.drain()
    authed_user = None
    while True:
        line = await reader.readline()
        if not line:
            break
        cmd = line.decode().strip()
        upper = cmd.upper()
        if upper.startswith("AUTHINFO USER"):
            authed_user = cmd.split(" ", 2)[2]
            writer.write(b"381 password required\r\n")
        elif upper.startswith("AUTHINFO PASS"):
            password = cmd.split(" ", 2)[2]
            if authed_user == GOOD_USER and password == GOOD_PASS:
                writer.write(b"281 authentication accepted\r\n")
            else:
                writer.write(b"481 authentication failed\r\n")
        elif upper.startswith("QUIT"):
            writer.write(b"205 bye\r\n")
            await writer.drain()
            break
        else:
            writer.write(b"500 unknown\r\n")
        await writer.drain()
    writer.close()


@pytest.fixture
async def nntp_server():
    server = await asyncio.start_server(_fake_nntp, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    yield port
    server.close()
    await server.wait_closed()


async def test_connect_without_auth_succeeds(nntp_server):
    result = await check_nntp("127.0.0.1", nntp_server, use_ssl=False)
    assert result["ok"] is True
    assert "200" in result["message"]


async def test_correct_credentials_accepted(nntp_server):
    result = await check_nntp(
        "127.0.0.1", nntp_server, use_ssl=False, username=GOOD_USER, password=GOOD_PASS
    )
    assert result["ok"] is True
    assert "281" in result["message"]


async def test_wrong_password_reports_auth_field(nntp_server):
    result = await check_nntp(
        "127.0.0.1", nntp_server, use_ssl=False, username=GOOD_USER, password="wrong"
    )
    assert result["ok"] is False
    assert result["field"] == "auth"
    assert "481" in result["message"]


async def test_direct_502_reject_reports_auth_field():
    """Real-world: Eweka answers AUTHINFO USER with 502 instead of RFC 481."""

    async def reject_502(reader, writer):
        writer.write(b"200 ready\r\n")
        await writer.drain()
        await reader.readline()
        writer.write(b"502 Authentication Failed\r\n")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(reject_502, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        result = await check_nntp("127.0.0.1", port, use_ssl=False, username="u", password="p")
        assert result["ok"] is False
        assert result["field"] == "auth"
        assert "502" in result["message"]
    finally:
        server.close()
        await server.wait_closed()


async def test_connection_refused_reports_host_field():
    result = await check_nntp("127.0.0.1", 1, use_ssl=False, timeout=2.0)
    assert result["ok"] is False
    assert result["field"] == "host"


async def test_unresolvable_host_reports_host_field():
    result = await check_nntp("no-such-host.invalid", 119, use_ssl=False, timeout=2.0)
    assert result["ok"] is False
    assert result["field"] == "host"


async def test_silent_server_times_out(unused_tcp_port_factory=None):
    async def silent(reader, writer):
        await asyncio.sleep(10)

    server = await asyncio.start_server(silent, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        result = await check_nntp("127.0.0.1", port, use_ssl=False, timeout=0.5)
        assert result["ok"] is False
        assert result["field"] == "host"
        assert "time" in result["message"].lower()
    finally:
        server.close()
        await server.wait_closed()
