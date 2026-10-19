"""Radarr / Sonarr integration: "in your library" badges and one-click add.

Spots carry an IMDb id (and sometimes a TVmaze id) in their website/description; we pull
those out at enrichment (and with the `external-ids` job for older spots). The arr libraries
are listed through their v3 API (header X-Api-Key), indexed by those ids and cached for ten
minutes. Adding takes the app's own lookup result (IMDb or TVmaze) and posts it with your
quality profile and root folder, which is the way both apps expect an add to be made.

Requests go to the admin-configured address only, with fixed API paths, no redirects, and
errors that never contain the address or key.
"""

from __future__ import annotations

import re
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .cache import cached, invalidate
from .jobs import register
from .models import JobState, Spot

TIMEOUT = 15.0
CACHE_SECONDS = 600
_IMDB = re.compile(r"(?<![A-Za-z0-9])tt(\d{6,9})(?!\d)", re.I)
_TVMAZE = re.compile(r"tvmaze\.com/shows/(\d{1,9})", re.I)


def extract_ids(*texts: str) -> tuple[str, str]:
    """(imdb digits, tvmaze digits) found in the given texts; '' when absent."""
    blob = " ".join(t for t in texts if t)
    imdb = _IMDB.search(blob)
    tvmaze = _TVMAZE.search(blob)
    return (imdb.group(1) if imdb else ""), (tvmaze.group(1) if tvmaze else "")


class ArrError(Exception):
    """A failure whose message is safe to show (no address, no key)."""


class ArrClient:
    def __init__(self, kind: str, url: str, apikey: str):
        if kind not in ("radarr", "sonarr"):
            raise ValueError(kind)
        self.kind, self.url, self.apikey = kind, url.rstrip("/"), apikey

    async def _call(self, method: str, path: str, **kwargs: Any) -> Any:
        if not self.url or not self.apikey:
            raise ArrError(f"{self.kind.title()} is not configured")
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False) as client:
                resp = await client.request(
                    method, f"{self.url}/api/v3{path}", headers={"X-Api-Key": self.apikey}, **kwargs
                )
        except httpx.HTTPError as exc:  # never str(exc): it embeds the URL
            raise ArrError(f"Could not reach {self.kind.title()} ({type(exc).__name__})") from exc
        if resp.status_code == 401:
            raise ArrError(f"{self.kind.title()} rejected the API key")
        if resp.status_code >= 400:
            raise ArrError(
                _validation_message(resp) or f"{self.kind.title()} answered HTTP {resp.status_code}"
            )
        try:
            return resp.json()
        except ValueError as exc:
            raise ArrError(
                f"{self.kind.title()} did not answer with JSON; check the address"
            ) from exc

    async def status(self) -> dict[str, Any]:
        data = await self._call("GET", "/system/status")
        return {
            "app": str(data.get("appName", self.kind))[:30],
            "version": str(data.get("version", ""))[:20],
        }

    async def options(self) -> dict[str, list[dict[str, Any]]]:
        profiles = await self._call("GET", "/qualityprofile")
        roots = await self._call("GET", "/rootfolder")
        return {
            "profiles": [
                {"id": p["id"], "name": str(p.get("name", ""))[:60]} for p in profiles if "id" in p
            ],
            "roots": [{"path": str(r["path"])[:200]} for r in roots if "path" in r],
        }

    async def library(self) -> dict[str, set[str]]:
        items = await self._call("GET", "/movie" if self.kind == "radarr" else "/series")
        out: dict[str, set[str]] = {"imdb": set(), "tvmaze": set()}
        for item in items if isinstance(items, list) else []:
            imdb = str(item.get("imdbId") or "").lower().removeprefix("tt")
            if imdb.isdigit():
                out["imdb"].add(imdb)
            maze = item.get("tvMazeId")
            if isinstance(maze, int) and maze > 0:
                out["tvmaze"].add(str(maze))
        return out

    async def add(self, imdb: str, tvmaze: str, profile_id: int, root: str) -> str:
        """Add the title to the library; returns a short message. 'Already…' counts as success."""
        found = await self._lookup(imdb, tvmaze)
        if not found:
            raise ArrError(f"{self.kind.title()} does not know this title")
        if (found.get("id") or 0) > 0:
            return f"Already in {self.kind.title()}"
        body = dict(found)
        body.update({"qualityProfileId": profile_id, "rootFolderPath": root, "monitored": True})
        if self.kind == "radarr":
            body.update({"minimumAvailability": "released", "addOptions": {"searchForMovie": True}})
        else:
            body.update(
                {
                    "seasonFolder": True,
                    "seriesType": body.get("seriesType") or "standard",
                    "seasons": [{**s, "monitored": True} for s in body.get("seasons", [])],
                    "addOptions": {"monitor": "all", "searchForMissingEpisodes": True},
                }
            )
        try:
            await self._call("POST", "/movie" if self.kind == "radarr" else "/series", json=body)
        except ArrError as exc:
            if "already" in str(exc).lower():
                return f"Already in {self.kind.title()}"
            raise
        invalidate("arr:")
        return f"Added to {self.kind.title()}"

    async def _lookup(self, imdb: str, tvmaze: str) -> dict[str, Any] | None:
        terms = []
        if imdb:
            terms.append(f"imdb:tt{imdb}")
        if tvmaze and self.kind == "sonarr":
            terms.append(f"tvmaze:{tvmaze}")
        for term in terms:
            path = "/movie/lookup" if self.kind == "radarr" else "/series/lookup"
            try:
                results = await self._call("GET", path, params={"term": term})
            except ArrError:
                continue
            if isinstance(results, list) and results and isinstance(results[0], dict):
                return results[0]
        return None


def _validation_message(resp: httpx.Response) -> str:
    """Radarr/Sonarr 400s are a list of {errorMessage}; pick them out."""
    try:
        data = resp.json()
    except ValueError:
        return ""
    if isinstance(data, list):
        msgs = [str(d.get("errorMessage", "")) for d in data if isinstance(d, dict)]
        return "; ".join(m for m in msgs if m)[:200]
    return ""


def configured(prefs: dict[str, Any], kind: str) -> bool:
    return bool(prefs.get(f"{kind}_url") and prefs.get(f"{kind}_apikey"))


def client_for(prefs: dict[str, Any], kind: str) -> ArrClient:
    return ArrClient(kind, prefs.get(f"{kind}_url", ""), prefs.get(f"{kind}_apikey", ""))


async def library_marks(prefs: dict[str, Any], spots: list[Spot]) -> set[int]:
    """Ids of the given spots that are already in Radarr or Sonarr. Never raises: an
    unreachable arr app just means no badges."""
    marks: set[int] = set()
    candidates = [s for s in spots if s.imdb_id or s.tvmaze_id]
    if not candidates:
        return marks
    for kind in ("radarr", "sonarr"):
        if not configured(prefs, kind):
            continue

        async def load(kind: str = kind) -> dict[str, set[str]]:
            try:
                return await client_for(prefs, kind).library()
            except ArrError:
                return {"imdb": set(), "tvmaze": set()}

        index = await cached(f"arr:{kind}", CACHE_SECONDS, load)
        for spot in candidates:
            if spot.imdb_id in index["imdb"] or (
                spot.tvmaze_id and spot.tvmaze_id in index["tvmaze"]
            ):
                marks.add(spot.id)
    return marks


def kind_for(spot: Spot) -> str:
    """Which arr app a spot belongs to: series (z1) -> Sonarr, otherwise Radarr."""
    return "sonarr" if "z1|" in (spot.subcat_z or "") else "radarr"


@register("external-ids", 1, "Finding IMDb/TVmaze ids")
async def _external_ids(session: AsyncSession) -> int:
    """Fill imdb_id/tvmaze_id on enriched spots from their stored website and description."""
    state = await session.get(JobState, "external-ids")
    cursor = int(state.detail) if state and state.detail.isdigit() else 0
    rows = (
        (
            await session.execute(
                select(Spot)
                .where(Spot.id > cursor, Spot.enriched != 0)
                .order_by(Spot.id)
                .limit(2000)
            )
        )
        .scalars()
        .all()
    )
    for spot in rows:
        imdb, tvmaze = extract_ids(spot.website, spot.description)
        if imdb and not spot.imdb_id:
            spot.imdb_id = imdb
        if tvmaze and not spot.tvmaze_id:
            spot.tvmaze_id = tvmaze
    if rows and state is not None:
        state.detail = str(rows[-1].id)  # resume point; run_jobs clears it when a run starts
    await session.commit()
    return len(rows)
