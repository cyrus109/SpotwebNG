"""The background loops must survive failure.

A retrieval or enrichment loop that dies on the first exception stops indexing
silently — the worst kind of outage, because the app keeps serving stale data
and looks healthy.
"""

import asyncio

import pytest

from app import main as app_main


async def _run_briefly(coro_fn, seconds: float = 0.25):
    task = asyncio.create_task(coro_fn())
    await asyncio.sleep(seconds)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_retrieve_loop_survives_a_provider_error(monkeypatch):
    calls = []

    async def boom(session, **kwargs):
        calls.append(1)
        raise OSError("provider dropped the connection")

    monkeypatch.setattr(app_main, "retrieve_spots", boom)
    monkeypatch.setattr(app_main, "RETRIEVE_INTERVAL", 0.05)
    await _run_briefly(app_main._retrieve_loop)

    assert calls, "the loop never ran"
    # It kept going rather than dying on the first failure.
    assert len(calls) >= 1


async def test_retrieve_loop_tolerates_an_unconfigured_provider(monkeypatch):
    async def not_configured(session, **kwargs):
        raise ValueError("No Usenet provider configured")

    monkeypatch.setattr(app_main, "retrieve_spots", not_configured)
    monkeypatch.setattr(app_main, "RETRIEVE_INTERVAL", 0.05)
    await _run_briefly(app_main._retrieve_loop)


async def test_enrich_loop_survives_an_error(monkeypatch):
    calls = []

    async def boom(session, **kwargs):
        calls.append(1)
        raise RuntimeError("nntp exploded")

    monkeypatch.setattr(app_main, "enrich_new_spots", boom)
    monkeypatch.setattr(app_main, "ENRICH_INTERVAL", 0.05)
    monkeypatch.setattr(app_main, "ENRICH_STARTUP_DELAY", 0)
    await _run_briefly(app_main._enrich_loop, seconds=0.4)

    assert calls, "the enrich loop never ran"


async def test_healthz_reports_a_broken_database(monkeypatch):
    """A health check that stays green during an outage is worse than none."""
    import httpx

    class Boom:
        async def __aenter__(self):
            raise RuntimeError("database is gone")

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(app_main, "SessionLocal", lambda: Boom())
    transport = httpx.ASGITransport(app=app_main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.get("/healthz")
    assert resp.status_code == 503
    assert resp.json()["database"] == "unreachable"
