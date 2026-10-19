"""Hand a spot's NZB to a downloader: SABnzbd's API, or a "black hole" folder.

Modelled on Spotweb's NzbHandler (push-sabnzbd and save): the server assembles
the NZB itself, so SABnzbd never needs to reach this app. The SABnzbd key travels
in the POST body, not the URL, because httpx logs request URLs. Error text shown
to the user never contains the URL or the key.
"""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from .downloads import record_download
from .models import Spot

SAB_TIMEOUT = 30.0
_UNSAFE = re.compile(r"[^A-Za-z0-9 ._()\[\]\-]")


def clean_name(title: str, limit: int = 120) -> str:
    """A filesystem- and header-safe job name (Spotweb whitelists the same way)."""
    return _UNSAFE.sub("_", title).strip(" .")[:limit] or "spot"


def _fail(message: str) -> dict[str, Any]:
    return {"ok": False, "message": message}


async def _sab_call(url: str, form: dict[str, str], files: dict | None = None) -> dict[str, Any]:
    """POST to SABnzbd's /api; returns the decoded JSON or {'error': ...}."""
    try:
        async with httpx.AsyncClient(timeout=SAB_TIMEOUT, follow_redirects=False) as client:
            resp = await client.post(f"{url}/api", data=form, files=files)
    except httpx.HTTPError as exc:  # never str(exc): it embeds the URL
        return {"error": f"Could not reach SABnzbd ({type(exc).__name__})"}
    if resp.status_code != 200:
        return {"error": f"SABnzbd answered HTTP {resp.status_code}"}
    try:
        data = resp.json()
    except ValueError:
        return {"error": "SABnzbd did not answer with JSON; check the URL"}
    return data if isinstance(data, dict) else {"error": "Unexpected SABnzbd answer"}


def _sab_error(data: dict[str, Any]) -> str:
    return str(data.get("error") or "SABnzbd refused the request")[:200]


async def check_sabnzbd(url: str, apikey: str) -> dict[str, Any]:
    if not url:
        return _fail("Enter the SABnzbd URL first")
    data = await _sab_call(url, {"mode": "queue", "apikey": apikey, "output": "json", "limit": "1"})
    queue = data.get("queue")
    if not isinstance(queue, dict):
        return _fail(_sab_error(data))
    cats = await _sab_call(url, {"mode": "get_cats", "apikey": apikey, "output": "json"})
    categories = [c for c in cats.get("categories", []) if isinstance(c, str)][:50]
    return {
        "ok": True,
        "message": f"Connected to SABnzbd {str(queue.get('version', ''))[:20]}".strip(),
        "categories": categories,
    }


async def send_to_sabnzbd(prefs: dict[str, Any], name: str, nzb: bytes) -> dict[str, Any]:
    if not prefs["sab_url"] or not prefs["sab_apikey"]:
        return _fail("SABnzbd is not configured (Settings → Download client)")
    form = {
        "mode": "addfile",
        "apikey": prefs["sab_apikey"],
        "output": "json",
        "nzbname": name,
        "priority": str(prefs["sab_priority"]),
    }
    if prefs["sab_category"]:
        form["cat"] = prefs["sab_category"]
    data = await _sab_call(
        prefs["sab_url"], form, files={"nzbfile": (f"{name}.nzb", nzb, "application/x-nzb")}
    )
    # SABnzbd can answer status:true without queueing anything, so require an id.
    if data.get("status") is True and data.get("nzo_ids"):
        return {"ok": True, "message": "Sent to SABnzbd"}
    return _fail(_sab_error(data))


def _write_nzb(directory: Path, name: str, spot_id: int, nzb: bytes) -> str:
    for filename in (f"{name}.nzb", f"{name}-{spot_id}.nzb"):
        try:
            with open(directory / filename, "xb") as fh:  # never overwrite
                fh.write(nzb)
            return filename
        except FileExistsError:
            continue
    raise FileExistsError(name)


async def send_to_blackhole(
    prefs: dict[str, Any], spot_id: int, name: str, nzb: bytes
) -> dict[str, Any]:
    raw = prefs["blackhole_dir"]
    directory = Path(raw) if raw else None
    if directory is None or not directory.is_absolute():
        return _fail("Set an absolute black-hole folder (Settings → Download client)")
    if not directory.is_dir() or not os.access(directory, os.W_OK):
        return _fail("The black-hole folder does not exist or is not writable")
    try:
        filename = await asyncio.to_thread(_write_nzb, directory, name, spot_id, nzb)
    except OSError as exc:
        return _fail(f"Could not write the NZB ({type(exc).__name__})")
    return {"ok": True, "message": f"Saved {filename}"}


async def send_spot(
    session: AsyncSession, spot: Spot, prefs: dict[str, Any], force: bool = False
) -> dict[str, Any]:
    from .health import COMPLETE, DEAD, PARTIAL, check_spot
    from .newznab import _real_nzb  # assembles (and caches) the real NZB

    action = prefs["nzb_action"]
    if action not in ("sabnzbd", "blackhole"):
        return _fail("Sending is turned off (Settings → Download client)")
    warning = ""
    policy = prefs["send_health_policy"]
    if policy != "off" and not force:
        health = await check_spot(session, spot)
        if health["health"] == DEAD and policy == "block-dead":
            return {
                "ok": False,
                "blocked": True,
                "message": f"Only {health['pct']}% of this spot's articles are still available",
            }
        if health["health"] == PARTIAL:
            warning = f" (partial: {health['pct']}% of articles available)"
        elif health["health"] not in (COMPLETE, PARTIAL, DEAD) and "error" in health:
            warning = " (availability could not be checked)"
    nzb = await _real_nzb(session, spot)
    if not nzb:
        return _fail("This spot's NZB is not available (yet); try again in a moment")
    name = clean_name(spot.title)
    if action == "sabnzbd":
        result = await send_to_sabnzbd(prefs, name, nzb)
    else:
        result = await send_to_blackhole(prefs, spot.id, name, nzb)
    if result["ok"]:
        await record_download(session, spot.message_id, "send")
        result["message"] += warning
    return result
