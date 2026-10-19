"""Sending an NZB to SABnzbd or a black-hole folder, and the settings behind it."""

import httpx
import pytest

from app.downloader import check_sabnzbd, clean_name, send_to_blackhole, send_to_sabnzbd
from app.settings_store import _coerce, backfill_plan

PREFS = {
    "sab_url": "http://sab:8080",
    "sab_apikey": "SECRETKEY",
    "sab_category": "movies",
    "sab_priority": 1,
    "blackhole_dir": "",
}


REAL_CLIENT = httpx.AsyncClient


def _mock(monkeypatch, handler):
    monkeypatch.setattr(
        "app.downloader.httpx.AsyncClient",
        lambda **kw: REAL_CLIENT(transport=httpx.MockTransport(handler), **kw),
    )


async def test_sabnzbd_gets_the_nzb_as_a_multipart_post_with_the_key_in_the_body(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"], seen["method"], seen["body"] = (
            str(request.url),
            request.method,
            request.content,
        )
        return httpx.Response(200, json={"status": True, "nzo_ids": ["SABnzbd_nzo_1"]})

    _mock(monkeypatch, handler)
    result = await send_to_sabnzbd(PREFS, "My Movie", b"<nzb/>")
    assert result["ok"] is True
    assert seen["method"] == "POST" and seen["url"] == "http://sab:8080/api"
    assert "SECRETKEY" not in seen["url"]  # URLs get logged; the body does not
    body = seen["body"]
    assert b'name="mode"' in body and b"addfile" in body and b"SECRETKEY" in body
    assert b'name="nzbfile"; filename="My Movie.nzb"' in body
    assert b"movies" in body and b"<nzb/>" in body


@pytest.mark.parametrize(
    "response, expected",
    [
        (
            httpx.Response(200, json={"status": False, "error": "API Key Incorrect"}),
            "API Key Incorrect",
        ),
        (httpx.Response(200, json={"status": True, "nzo_ids": []}), "refused"),  # no job queued
        (httpx.Response(403, text="nope"), "HTTP 403"),
        (httpx.Response(200, text="<html>login</html>"), "JSON"),
    ],
)
async def test_sabnzbd_failures_are_reported_plainly(monkeypatch, response, expected):
    _mock(monkeypatch, lambda request: response)
    result = await send_to_sabnzbd(PREFS, "x", b"n")
    assert result["ok"] is False and expected in result["message"]


async def test_connection_errors_never_leak_the_url_or_key(monkeypatch):
    def handler(request):
        raise httpx.ConnectError("boom http://sab:8080/api?apikey=SECRETKEY")

    _mock(monkeypatch, handler)
    result = await send_to_sabnzbd(PREFS, "x", b"n")
    assert result["ok"] is False
    assert "SECRETKEY" not in result["message"] and "sab:8080" not in result["message"]


async def test_the_connection_test_reads_version_and_categories(monkeypatch):
    def handler(request):
        mode = request.content.decode()
        if "mode=queue" in mode:
            return httpx.Response(200, json={"queue": {"version": "4.3.1"}})
        return httpx.Response(200, json={"categories": ["*", "movies", "tv"]})

    _mock(monkeypatch, handler)
    result = await check_sabnzbd("http://sab:8080", "k")
    assert result == {
        "ok": True,
        "message": "Connected to SABnzbd 4.3.1",
        "categories": ["*", "movies", "tv"],
    }

    _mock(
        monkeypatch,
        lambda r: httpx.Response(200, json={"status": False, "error": "API Key Incorrect"}),
    )
    assert (await check_sabnzbd("http://sab:8080", "bad"))["ok"] is False


async def test_blackhole_writes_safe_names_and_never_overwrites(tmp_path):
    prefs = {**PREFS, "blackhole_dir": str(tmp_path)}
    name = clean_name("../../etc/passwd: évil*?")
    assert "/" not in name and not name.startswith(".")
    first = await send_to_blackhole(prefs, 7, name, b"one")
    second = await send_to_blackhole(prefs, 8, name, b"two")
    assert first["ok"] and second["ok"]
    files = sorted(p.name for p in tmp_path.iterdir())
    assert len(files) == 2 and any(f.endswith("-8.nzb") for f in files)
    assert all(p.parent == tmp_path for p in tmp_path.iterdir())


async def test_blackhole_needs_a_real_absolute_writable_folder(tmp_path):
    for bad in ("", "relative/dir", str(tmp_path / "missing")):
        result = await send_to_blackhole({**PREFS, "blackhole_dir": bad}, 1, "x", b"n")
        assert result["ok"] is False


def test_sab_url_is_validated_and_secrets_are_not_echoed_by_defaults():
    assert _coerce("sab_url", "http://sab:8080/") == "http://sab:8080"
    assert _coerce("sab_url", "javascript:alert(1)") == ""
    assert _coerce("sab_url", "http://a b") == ""
    assert _coerce("sab_category", "mov\r\nies") == "movies"
    assert _coerce("sab_priority", "99") == 2 and _coerce("nzb_action", "rm -rf") == "download"


def test_backfill_plan_articles_and_days():
    base = {"retrieve_all": False, "retrieve_days": 0, "retrieve_backfill": 20000}
    assert backfill_plan({**base, "retrieve_backfill_unit": "articles"}) == {
        "max_backfill": 20000,
        "retrieve_all": False,
        "max_age_days": 0,
    }
    days = backfill_plan({**base, "retrieve_backfill": 45, "retrieve_backfill_unit": "days"})
    assert days["retrieve_all"] is True and days["max_age_days"] == 45
    # an explicit full-history run keeps its own day limit
    full = backfill_plan(
        {
            **base,
            "retrieve_all": True,
            "retrieve_days": 90,
            "retrieve_backfill": 45,
            "retrieve_backfill_unit": "days",
        }
    )
    assert full["max_age_days"] == 90
    huge = backfill_plan({**base, "retrieve_backfill": 10**8, "retrieve_backfill_unit": "days"})
    assert huge["max_age_days"] == 36500
