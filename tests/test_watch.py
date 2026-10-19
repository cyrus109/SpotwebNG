"""Watchlists, notifications and failure alerts."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import notify as notify_mod
from app import spotters, watch
from app.downloads import record_download
from app.models import Base, Download, Setting, Spot, Watch, WatchHit
from app.notify import FailureTracker, channels, notify

REAL_CLIENT = httpx.AsyncClient
NOW = datetime.now(UTC)


def _mock(monkeypatch, handler):
    monkeypatch.setattr(
        "app.notify.httpx.AsyncClient",
        lambda **kw: REAL_CLIENT(transport=httpx.MockTransport(handler), **kw),
    )


ALL = {
    "notify_webhook_url": "https://hook.example/h?token=SECRET1",
    "notify_discord_url": "https://discord.example/api/webhooks/1/SECRET2",
    "notify_ntfy_url": "https://ntfy.example/topic",
    "notify_telegram_token": "123:SECRET3",
    "notify_telegram_chat": "42",
}


async def test_every_channel_gets_its_own_format(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen[request.url.host] = request
        return httpx.Response(200)

    _mock(monkeypatch, handler)
    results = await notify(ALL, "Title\r\nInjected: header", "Body text", "https://app/spot/1")
    assert set(results) == {"webhook", "discord", "ntfy", "telegram"} and all(
        r["ok"] for r in results.values()
    )
    import json

    assert json.loads(seen["hook.example"].content) == {
        "title": "Title  Injected: header",  # line breaks in a title never survive
        "text": "Body text",
        "url": "https://app/spot/1",
    }
    assert "Body text" in json.loads(seen["discord.example"].content)["content"]
    ntfy = seen["ntfy.example"]
    assert (
        ntfy.content == b"Body text"
        and "\r" not in ntfy.headers["title"]
        and "\n" not in ntfy.headers["title"]
    )
    assert ntfy.headers["click"] == "https://app/spot/1"
    tg = seen["api.telegram.org"]
    assert (
        tg.url.path == "/bot123:SECRET3/sendMessage" and json.loads(tg.content)["chat_id"] == "42"
    )


async def test_failures_never_leak_urls_or_tokens(monkeypatch):
    def handler(request):
        if request.url.host == "hook.example":
            raise httpx.ConnectError(f"boom {request.url}")
        return httpx.Response(401)

    _mock(monkeypatch, handler)
    results = await notify(ALL, "t", "x")
    flat = str(results)
    for secret in ("SECRET1", "SECRET2", "SECRET3", "hook.example"):
        assert secret not in flat
    assert results["webhook"]["ok"] is False and results["discord"]["message"] == "HTTP 401"


def test_channels_need_complete_settings():
    assert channels({}) == []
    assert channels({"notify_telegram_token": "t"}) == []  # no chat id
    assert channels(ALL) == ["webhook", "discord", "ntfy", "telegram"]


async def test_failure_alert_fires_once_and_recovery_once(monkeypatch):
    sent = []

    async def fake_notify(prefs, title, text, link=""):
        sent.append(title)
        return {}

    monkeypatch.setattr(notify_mod, "notify", fake_notify)
    prefs = {**ALL, "notify_failure_after": 3}
    tracker = FailureTracker()
    for _ in range(5):
        await tracker.failed(prefs, "NntpError")
    assert sent == ["SpotwebNG: retrieval is failing"]  # once, at the 3rd failure
    await tracker.succeeded(prefs)
    await tracker.succeeded(prefs)
    assert sent[-1] == "SpotwebNG: retrieval recovered" and len(sent) == 2
    off = FailureTracker()
    for _ in range(9):
        await off.failed({**ALL, "notify_failure_after": 0}, "X")
    assert len(sent) == 2


@pytest.fixture
async def factory():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
def captured(monkeypatch):
    calls = []

    async def fake_notify(prefs, title, text, link=""):
        calls.append((title, text, link))
        return {}

    monkeypatch.setattr(watch, "notify", fake_notify)
    return calls


async def _prefs(factory, **values):
    async with factory() as s:
        for k, v in {
            "notify_webhook_url": "https://hook.example/x",
            "public_url": "https://sw.example",
            **values,
        }.items():
            s.add(Setting(key=k, value=v))
        await s.commit()


async def _spot(factory, title, **kw):
    kw.setdefault("posted_at", NOW - timedelta(hours=1))
    kw.setdefault("size", 2 * 1024**3)
    async with factory() as s:
        spot = Spot(message_id=f"<{title.replace(' ', '')}@x>", title=title, **kw)
        s.add(spot)
        await s.commit()
        return spot.id


async def test_a_new_watch_ignores_existing_spots_and_catches_new_ones(factory, captured):
    await _prefs(factory)
    await _spot(factory, "Dune Part One 1080p")
    async with factory() as s:
        await watch.new_watch(s, name="dune", query="dune")
        assert (await watch.run_watches(s))["matched"] == 0  # history is not news
    await _spot(factory, "Dune Part Two 2160p")
    await _spot(factory, "Something Else")
    async with factory() as s:
        totals = await watch.run_watches(s)
        again = await watch.run_watches(s)
        hits = (await s.execute(select(WatchHit))).scalars().all()
    assert totals["matched"] == 1 and totals["notified"] == 1 and again["matched"] == 0
    assert [h.title for h in hits] == ["Dune Part Two 2160p"] and hits[0].outcome == "notified"
    assert captured[0][0] == "dune: Dune Part Two 2160p" and captured[0][2].startswith(
        "https://sw.example/spot/"
    )


async def test_send_action_sends_records_the_download_and_never_repeats(
    factory, captured, tmp_path, monkeypatch
):
    async def fake_nzb(session, spot):
        return b"<nzb/>"

    monkeypatch.setattr("app.newznab._real_nzb", fake_nzb)
    await _prefs(
        factory, nzb_action="blackhole", blackhole_dir=str(tmp_path), send_health_policy="off"
    )
    async with factory() as s:
        await watch.new_watch(s, name="movies", tree="", query="movie", action="send")
    await _spot(factory, "Great Movie 1080p")
    async with factory() as s:
        totals = await watch.run_watches(s)
        await watch.run_watches(s)
        downloads = (await s.execute(select(Download))).scalars().all()
    assert totals["sent"] == 1 and len(list(tmp_path.iterdir())) == 1
    assert len(downloads) == 1 and downloads[0].source == "send"
    assert "sent:" in captured[0][1]


async def test_already_downloaded_spots_are_skipped(factory, captured):
    await _prefs(factory)
    async with factory() as s:
        await watch.new_watch(s, name="w", query="alpha")
    await _spot(factory, "Alpha One")
    async with factory() as s:
        await record_download(s, "<AlphaOne@x>", "api")
        totals = await watch.run_watches(s)
        hit = (await s.execute(select(WatchHit))).scalar_one()
    assert (
        totals["skipped"] == 1 and hit.outcome == "skipped: already downloaded" and captured == []
    )


async def test_watches_respect_adult_old_blocked_and_size_rules(factory, captured):
    await _prefs(factory)
    async with factory() as s:
        await watch.new_watch(s, name="w", query="show", min_size=1024**3)
    await _spot(factory, "Show Adult", category=6040)
    await _spot(factory, "Show Old", posted_at=NOW - timedelta(days=40))
    await _spot(factory, "Show Tiny", size=100)
    await _spot(factory, "Show Spam", spotter_id="SPAMID")
    await _spot(factory, "Show Good")
    async with factory() as s:
        await spotters.set_status(s, "SPAMID", "blocked")
        await watch.run_watches(s)
        titles = [h.title for h in (await s.execute(select(WatchHit))).scalars().all()]
    assert titles == ["Show Good"]


async def test_a_full_batch_continues_next_run_without_losing_or_repeating(
    factory, captured, monkeypatch
):
    monkeypatch.setattr(watch, "PER_RUN", 3)
    await _prefs(factory)
    async with factory() as s:
        await watch.new_watch(s, name="w", query="batch")
    for i in range(8):
        await _spot(factory, f"Batch {i}")
    seen = []
    for _ in range(5):
        async with factory() as s:
            await watch.run_watches(s)
            seen = [
                h.title
                for h in (await s.execute(select(WatchHit).order_by(WatchHit.id))).scalars().all()
            ]
    assert seen == [f"Batch {i}" for i in range(8)]


async def test_an_imported_watch_starts_from_now(factory, captured):
    await _prefs(factory)
    await _spot(factory, "Old Match One")
    async with factory() as s:
        s.add(Watch(name="imported", query="match", last_id=-1))
        await s.commit()
        assert (await watch.run_watches(s))["matched"] == 0
        (await s.execute(select(Watch))).scalar_one()
    await _spot(factory, "New Match Two")
    async with factory() as s:
        assert (await watch.run_watches(s))["matched"] == 1


async def test_untrusted_titles_cannot_ping_or_format_a_discord_channel(monkeypatch):
    import json

    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(204)

    _mock(monkeypatch, handler)
    evil = "@everyone **click** [here](http://x) _y_ `z`"
    await notify(
        {"notify_discord_url": "https://discord.example/hook"},
        evil,
        f"{evil}\x00\x1b",
        "https://app/spot/1",
    )
    sent = seen["body"]
    assert sent["allowed_mentions"] == {"parse": []}
    assert "@everyone" not in sent["content"] and "@​everyone" in sent["content"]
    assert "**click**" not in sent["content"] and "\x00" not in sent["content"]
