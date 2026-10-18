"""The background loops must survive failure.

A retrieval or enrichment loop that dies on the first exception stops indexing
silently — the worst kind of outage, because the app keeps serving stale data
and looks healthy.
"""

import asyncio
from contextlib import asynccontextmanager

import pytest

from app import main as app_main


@pytest.fixture(autouse=True)
def no_real_database(monkeypatch):
    """The loops open their own sessions. Give them a stub so a test never
    touches a real SQLite file (which on CI does not exist and can block)."""

    @asynccontextmanager
    async def fake_session():
        yield object()

    monkeypatch.setattr(app_main, "SessionLocal", fake_session)
    monkeypatch.setattr(app_main, "load_settings", _fake_settings)
    monkeypatch.setattr(app_main, "prune_old_spots", _noop)
    monkeypatch.setattr(app_main, "sweep_expired_sessions", _noop)


async def _fake_settings(session):
    return {
        "retrieve_interval": 1,
        "retrieve_backfill": 100,
        "retrieve_all": False,
        "retrieve_comments": False,
        "retention_days": 0,
        "retention_type": "fullonly",
        "auto_enrich": True,
    }


async def _noop(session, *args, **kwargs):
    return {"deleted": 0, "stripped": 0}


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
