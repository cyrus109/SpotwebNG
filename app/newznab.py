"""Newznab-compatible API.

This is the contract for Newznab clients (Radarr, Sonarr, Prowlarr, etc.).
Endpoint: GET /api?t=caps|search|tvsearch|movie|get&apikey=...
Returns Newznab XML (the schema those clients parse: <channel><item> with
<enclosure> + <newznab:attr>).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from email.utils import format_datetime
from xml.etree.ElementTree import Element, SubElement, register_namespace, tostring

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .categories import CATEGORIES
from .config import NZB_DIR, get_settings
from .db import get_session
from .models import ApiUser, Spot
from .settings_store import load_settings
from .spots import (
    ADULT_CATEGORY_HIGH,
    ADULT_CATEGORY_LOW,
    get_spot_by_message_id,
    is_adult_spot,
    search_spots,
)


async def _real_nzb(session: AsyncSession, spot: Spot) -> bytes | None:
    """Assemble the real NZB from Usenet segments (cached on disk)."""
    if not spot.nzb_segments:
        return None
    NZB_DIR.mkdir(parents=True, exist_ok=True)
    cached = NZB_DIR / f"{spot.id}.nzb"
    if cached.exists():
        return cached.read_bytes()

    from .retriever import NntpClient, NntpError, _load_provider, fetch_binary

    try:
        provider = await _load_provider(session)
        client = NntpClient()
        await client.connect(
            provider["provider_host"],
            int(provider.get("provider_port", 119)),
            provider.get("provider_ssl", "yes") == "yes",
            provider.get("username", ""),
            provider.get("password", ""),
        )
        try:
            nzb = await fetch_binary(client, json.loads(spot.nzb_segments), compressed=True)
        finally:
            await client.quit()
    except (NntpError, OSError, ValueError):
        return None
    if nzb:
        cached.write_bytes(nzb)
    return nzb


router = APIRouter(prefix="/api", tags=["newznab"])

NEWZNAB_NS = "http://www.newznab.com/DTD/2010/feeds/attributes/"
# Emit the conventional "newznab:" prefix (not ElementTree's auto "ns0:") so naive
# string-matching Newznab clients in the wild parse <newznab:attr> correctly.
register_namespace("newznab", NEWZNAB_NS)


def _xml_response(root: Element) -> Response:
    body = b'<?xml version="1.0" encoding="UTF-8"?>\n' + tostring(root, encoding="utf-8")
    return Response(content=body, media_type="application/xml")


def _error(code: int, description: str) -> Response:
    root = Element("error", {"code": str(code), "description": description})
    return _xml_response(root)


async def _touch_key(session: AsyncSession, key: str) -> None:
    row = (
        await session.execute(select(ApiUser).where(ApiUser.api_key == key))
    ).scalar_one_or_none()
    if row is not None:
        row.last_used = datetime.now(UTC)
        await session.commit()


async def _valid_key(apikey: str, session: AsyncSession) -> bool:
    res = await session.execute(select(ApiUser).where(ApiUser.api_key == apikey))
    return res.scalar_one_or_none() is not None


def _caps_xml(hide_adult: bool = False) -> Response:
    root = Element("caps")
    server = SubElement(root, "server")
    server.set("title", get_settings().app_name)
    SubElement(root, "limits", {"max": "200", "default": "100"})
    searching = SubElement(root, "searching")
    SubElement(searching, "search", {"available": "yes", "supportedParams": "q"})
    SubElement(
        searching,
        "tv-search",
        {"available": "yes", "supportedParams": "q,season,ep,tvmazeid,rid"},
    )
    SubElement(searching, "movie-search", {"available": "yes", "supportedParams": "q,imdbid"})
    # Lidarr and Readarr read these and refuse an indexer that omits their mode.
    SubElement(searching, "audio-search", {"available": "yes", "supportedParams": "q,artist,album"})
    SubElement(searching, "book-search", {"available": "yes", "supportedParams": "q,author,title"})
    cats = SubElement(root, "categories")
    parents: dict[int, Element] = {}
    for cid, name, parent in CATEGORIES:
        # Never advertise a category the instance refuses to serve — a client
        # that maps to it would only ever get empty results.
        if hide_adult and ADULT_CATEGORY_LOW <= cid <= ADULT_CATEGORY_HIGH:
            continue
        if parent == 0:
            el = SubElement(cats, "category", {"id": str(cid), "name": name})
            parents[cid] = el
        else:
            host = parents.get(parent, cats)
            SubElement(host, "subcat", {"id": str(cid), "name": name})
    return _xml_response(root)


def _spot_to_item(channel: Element, spot: Spot, base_url: str, apikey: str = "") -> None:
    item = SubElement(channel, "item")
    SubElement(item, "title").text = spot.title
    SubElement(item, "guid").text = spot.message_id
    dl = f"{base_url}/api?t=get&id={spot.id}&apikey={apikey}"
    SubElement(item, "link").text = dl
    if spot.posted_at:
        SubElement(item, "pubDate").text = format_datetime(spot.posted_at)
    SubElement(item, "category").text = str(spot.category)
    if spot.description:
        SubElement(item, "description").text = spot.description
    SubElement(
        item,
        "enclosure",
        {"url": dl, "length": str(spot.size), "type": "application/x-nzb"},
    )

    def attr(name: str, value: str) -> None:
        SubElement(item, f"{{{NEWZNAB_NS}}}attr", {"name": name, "value": value})

    attr("category", str(spot.category))
    attr("size", str(spot.size))
    if spot.imdb_id:
        attr("imdb", spot.imdb_id)
    if spot.poster_url:
        attr("coverurl", spot.poster_url)


@router.get("")
async def newznab(
    request: Request,
    t: str = Query("", description="function: caps|search|tvsearch|movie|get|details"),
    apikey: str = Query(""),
    q: str = Query(""),
    imdbid: str = Query(""),
    # Clients send their own field names rather than a bare q.
    artist: str = Query(""),  # Lidarr
    album: str = Query(""),  # Lidarr
    author: str = Query(""),  # Readarr
    title: str = Query(""),  # Readarr
    season: str = Query(""),  # Sonarr
    ep: str = Query(""),  # Sonarr
    cat: str = Query(""),
    id: str = Query(""),
    offset: int = Query(0),
    limit: int = Query(100),
    session: AsyncSession = Depends(get_session),
) -> Response:
    # caps requires no auth (clients probe it before configuring a key).
    prefs = await load_settings(session)
    hide_adult = bool(prefs["hide_adult"])

    if t in ("caps", "c"):
        return _caps_xml(hide_adult)

    if not apikey or not await _valid_key(apikey, session):
        return _error(100, "Incorrect user credentials")
    await _touch_key(session, apikey)

    base_url = str(request.base_url).rstrip("/")

    if t in ("get", "g"):
        if not id:
            return _error(200, "Missing parameter (id)")
        spot = (
            await session.get(Spot, int(id))
            if id.isdigit()
            else await get_spot_by_message_id(session, id)
        )
        if spot is None or (hide_adult and is_adult_spot(spot)):
            return _error(300, "No such item")
        real = await _real_nzb(session, spot)
        if real is not None:
            return Response(
                content=real,
                media_type="application/x-nzb",
                headers={"Content-Disposition": f'attachment; filename="{spot.id}.nzb"'},
            )
        # Fallback: pointer stub (spot not yet enriched or segments unavailable).
        nzb = (
            f'<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<!DOCTYPE nzb PUBLIC "-//newzBin//DTD NZB 1.1//EN" '
            f'"http://www.newzbin.com/DTD/nzb/nzb-1.1.dtd">\n'
            f'<nzb xmlns="http://www.newzbin.com/DTD/2003/nzb">\n'
            f'  <head><meta type="title">{spot.title}</meta>'
            f'<meta type="messageid">{spot.nzb_ref}</meta></head>\n'
            f"</nzb>\n"
        )
        return Response(content=nzb, media_type="application/x-nzb")

    if t in ("search", "s", "tvsearch", "t", "movie", "m", "music", "a", "book", "b"):
        cats = [int(c) for c in cat.split(",") if c.strip().isdigit()] or None
        # Fold the client-specific fields into one search term.
        terms = [part for part in (q, artist, album, author, title) if part]
        if t in ("tvsearch", "t") and season:
            terms.append(f"S{int(season):02d}" if season.isdigit() else season)
            if ep:
                terms[-1] += f"E{int(ep):02d}" if ep.isdigit() else ep
        q = " ".join(terms)
        rows, total = await search_spots(
            session,
            query=q or None,
            categories=cats,
            imdb_id=imdbid or None,
            exclude_adult=hide_adult,
            offset=offset,
            limit=limit,
        )
        # namespace declaration comes from register_namespace — adding it manually
        # too made every response invalid XML (duplicate xmlns:newznab attribute)
        root = Element("rss", {"version": "2.0"})
        channel = SubElement(root, "channel")
        SubElement(channel, "title").text = get_settings().app_name
        SubElement(
            channel,
            f"{{{NEWZNAB_NS}}}response",
            {"offset": str(offset), "total": str(total)},
        )
        for spot in rows:
            _spot_to_item(channel, spot, base_url, apikey)
        return _xml_response(root)

    if not t:
        return _error(200, "Missing parameter (t)")
    return _error(202, "No such function")
