"""Radarr/Sonarr: id extraction, the v3 API calls, badges and one-click add."""

import json

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import jobs, library
from app.library import ArrClient, ArrError, extract_ids
from app.models import Base, JobState, Spot

REAL_CLIENT = httpx.AsyncClient


def _mock(monkeypatch, handler):
    monkeypatch.setattr(
        "app.library.httpx.AsyncClient",
        lambda **kw: REAL_CLIENT(transport=httpx.MockTransport(handler), **kw),
    )


@pytest.mark.parametrize(
    "text, expected",
    [
        ("http://www.imdb.com/title/tt0113277/", ("0113277", "")),
        ("see tt1234567 and https://www.tvmaze.com/shows/2993/stranger", ("1234567", "2993")),
        ("att1234567 or tt123", ("", "")),  # glued to letters / too short
        ("tt12345678901", ("", "")),  # too many digits
        ("", ("", "")),
    ],
)
def test_ids_are_pulled_from_links_and_text(text, expected):
    assert extract_ids("", text) == expected
    assert extract_ids(text, "") == expected


async def test_library_is_indexed_by_imdb_and_tvmaze(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-api-key"] == "KEY" and request.url.path == "/api/v3/series"
        return httpx.Response(
            200,
            json=[
                {"id": 1, "imdbId": "tt0903747", "tvMazeId": 169},
                {"id": 2, "imdbId": None, "tvMazeId": 0},
            ],
        )

    _mock(monkeypatch, handler)
    index = await ArrClient("sonarr", "http://sonarr:8989/", "KEY").library()
    assert index == {"imdb": {"0903747"}, "tvmaze": {"169"}}


async def test_adding_a_movie_posts_the_lookup_with_profile_root_and_search(monkeypatch):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(
            (request.method, request.url.path, request.url.params.get("term"), request.content)
        )
        if request.url.path.endswith("/movie/lookup"):
            return httpx.Response(
                200,
                json=[
                    {"title": "Heat", "tmdbId": 949, "imdbId": "tt0113277", "id": 0, "year": 1995}
                ],
            )
        return httpx.Response(201, json={"id": 7})

    _mock(monkeypatch, handler)
    message = await ArrClient("radarr", "http://radarr:7878", "K").add("0113277", "", 4, "/movies")
    assert message == "Added to Radarr" and calls[0][2] == "imdb:tt0113277"
    posted = json.loads(calls[1][3])
    assert calls[1][:2] == ("POST", "/api/v3/movie")
    assert posted["tmdbId"] == 949 and posted["title"] == "Heat"  # the lookup object is reused
    assert posted["qualityProfileId"] == 4 and posted["rootFolderPath"] == "/movies"
    assert posted["monitored"] is True and posted["addOptions"] == {"searchForMovie": True}
    assert posted["minimumAvailability"] == "released"


async def test_adding_a_series_monitors_every_season_and_searches(monkeypatch):
    def handler(request):
        if request.url.path.endswith("/series/lookup"):
            return httpx.Response(
                200,
                json=[
                    {
                        "title": "Breaking Bad",
                        "tvdbId": 81189,
                        "id": 0,
                        "seasons": [
                            {"seasonNumber": 1, "monitored": False},
                            {"seasonNumber": 2, "monitored": False},
                        ],
                    }
                ],
            )
        handler.body = json.loads(request.content)
        return httpx.Response(200, json={"id": 3})

    _mock(monkeypatch, handler)
    await ArrClient("sonarr", "http://s:8989", "K").add("0903747", "169", 2, "/tv")
    body = handler.body
    assert all(s["monitored"] for s in body["seasons"]) and body["seasonFolder"] is True
    assert body["addOptions"] == {"monitor": "all", "searchForMissingEpisodes": True}
    assert body["seriesType"] == "standard" and body["rootFolderPath"] == "/tv"


async def test_titles_already_in_the_library_are_a_success_not_an_error(monkeypatch):
    _mock(monkeypatch, lambda r: httpx.Response(200, json=[{"title": "Heat", "id": 12}]))
    assert (
        await ArrClient("radarr", "http://r", "K").add("0113277", "", 1, "/m")
        == "Already in Radarr"
    )

    def handler(request):
        if "lookup" in request.url.path:
            return httpx.Response(200, json=[{"title": "Heat", "id": 0}])
        return httpx.Response(400, json=[{"errorMessage": "This movie has already been added"}])

    _mock(monkeypatch, handler)
    assert (
        await ArrClient("radarr", "http://r", "K").add("0113277", "", 1, "/m")
        == "Already in Radarr"
    )


async def test_errors_are_plain_and_never_contain_the_address_or_key(monkeypatch):
    def refuse(request):
        raise httpx.ConnectError(f"boom {request.url} {request.headers['x-api-key']}")

    _mock(monkeypatch, refuse)
    with pytest.raises(ArrError) as exc:
        await ArrClient("radarr", "http://secret-host:7878", "SECRETKEY").status()
    assert "secret-host" not in str(exc.value) and "SECRETKEY" not in str(exc.value)

    _mock(monkeypatch, lambda r: httpx.Response(401))
    with pytest.raises(ArrError, match="rejected the API key"):
        await ArrClient("radarr", "http://r", "bad").status()
    with pytest.raises(ArrError, match="not configured"):
        await ArrClient("radarr", "", "").status()
    _mock(monkeypatch, lambda r: httpx.Response(200, text="<html>login</html>"))
    with pytest.raises(ArrError, match="JSON"):
        await ArrClient("sonarr", "http://s", "K").status()


async def test_badges_come_from_the_cached_library_and_survive_an_unreachable_app(monkeypatch):
    from app import cache

    monkeypatch.setattr(cache, "enabled", True)
    cache.invalidate()
    hits = []

    def handler(request):
        hits.append(request.url.path)
        return httpx.Response(200, json=[{"imdbId": "tt0113277"}])

    _mock(monkeypatch, handler)
    prefs = {"radarr_url": "http://r", "radarr_apikey": "K", "sonarr_url": "", "sonarr_apikey": ""}
    heat, other = Spot(id=1, imdb_id="0113277"), Spot(id=2, imdb_id="9999999")
    assert await library.library_marks(prefs, [heat, other]) == {1}
    assert await library.library_marks(prefs, [heat, other]) == {1} and hits == [
        "/api/v3/movie"
    ]  # cached
    cache.invalidate()
    _mock(monkeypatch, lambda r: (_ for _ in ()).throw(httpx.ConnectError("down")))
    assert await library.library_marks(prefs, [heat]) == set()  # no badges, no crash
    cache.invalidate()


@pytest.fixture
async def factory():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def test_the_job_fills_ids_for_spots_that_were_enriched_before(factory):
    from datetime import UTC, datetime

    when = datetime(2026, 7, 1, tzinfo=UTC)
    async with factory() as s:
        s.add(
            Spot(
                message_id="<a@x>",
                title="A",
                posted_at=when,
                enriched=1,
                website="http://www.imdb.com/title/tt0113277/",
            )
        )
        s.add(
            Spot(
                message_id="<b@x>",
                title="B",
                posted_at=when,
                enriched=1,
                description="https://www.tvmaze.com/shows/169/x",
            )
        )
        s.add(
            Spot(message_id="<c@x>", title="C", posted_at=when, enriched=0, website="tt7654321")
        )  # not enriched yet
        s.add(
            Spot(
                message_id="<d@x>",
                title="D",
                posted_at=when,
                enriched=1,
                imdb_id="1111111",
                website="tt2222222",
            )
        )
        await s.commit()
    saved = dict(jobs.REGISTRY)
    jobs.REGISTRY.clear()
    jobs.REGISTRY["external-ids"] = saved["external-ids"]
    try:
        await jobs.run_jobs(factory, pause=0)
    finally:
        jobs.REGISTRY.update(saved)
    async with factory() as s:
        rows = {
            r.title: (r.imdb_id, r.tvmaze_id)
            for r in (await s.execute(select(Spot))).scalars().all()
        }
        state = await s.get(JobState, "external-ids")
    assert rows == {"A": ("0113277", ""), "B": ("", "169"), "C": ("", ""), "D": ("1111111", "")}
    assert state.status == "done"
