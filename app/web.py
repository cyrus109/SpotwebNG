"""Web UI routes — the modern, Netflix-style browsing experience + setup wizard."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import shutil
import tempfile
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.templating import Jinja2Templates
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from . import spotters
from .auth import (
    ADMIN,
    COOKIE,
    ROLES,
    SESSION_DAYS,
    VIEWER,
    account_exists,
    clear_login_failures,
    count_admins,
    create_user,
    end_session,
    is_admin,
    login_locked,
    record_login_failure,
    revoke_all_sessions,
    set_password,
    start_session,
    user_for_session,
    verify_credentials,
)
from .autobackup import summary as backup_summary
from .backup import export_chunks, import_file
from .cache import cached
from .categories import CATEGORY_NAMES
from .comments import comment_counts, fetch_comment_bodies
from .config import BASE_DIR, IMAGES_DIR, get_settings, load_providers
from .csrf import csrf_context, verify_csrf
from .db import SessionLocal, get_session
from .downloader import check_sabnzbd, send_spot
from .downloads import downloaded_map
from .duplicates import best as dup_best
from .duplicates import release_counts, siblings
from .facets import option_counts, section_counts, section_dimensions
from .filters import (
    FILTER_SECTIONS,
    base_tree,
    clean_tree,
    section_for,
    tree_targets_adult,
    visible_sections,
)
from .health import check_spot
from .jobs import job_status
from .jobs import rerun as jobs_rerun
from .library import ArrClient, ArrError, kind_for, library_marks
from .library import client_for as arr_client_for
from .library import configured as arr_configured
from .maintenance import ACTIONS, DESTRUCTIVE, run_maintenance
from .models import ApiUser, Comment, Setting, Spot, User, Watch, WatchHit
from .nntp_check import check_nntp
from .notify import channels as notify_channels
from .notify import notify as send_notification
from .progress import LIVE, history_percent, live_idle, live_set, load_status, save_status
from .retention import retention_cutoff
from .retriever import RETRIEVE_LOCK, enrich_new_spots, retrieve_spots
from .secrets_store import SECRET_KEYS, all_settings, encrypt, public_settings
from .settings_store import backfill_plan, load_settings, save_settings
from .spots import get_spot, is_adult_spot, search_spots
from .stats import bars as stats_bars
from .stats import collect as stats_collect
from .version import VERSION
from .watch import ACTIONS as watch_actions
from .watch import new_watch

router = APIRouter(dependencies=[Depends(verify_csrf)])
templates = Jinja2Templates(
    directory=str(BASE_DIR / "templates"), context_processors=[csrf_context]
)


def _gb(size: int) -> str:
    return f"{size / 1024**3:.1f} GB" if size else "—"


templates.env.filters["gb"] = _gb
templates.env.filters["catname"] = lambda c: CATEGORY_NAMES.get(c, "Other")


def _asset_v(path: str) -> int:
    """Cache-buster: file mtime as version query, so browsers refetch changed assets."""
    try:
        return int((BASE_DIR / "static" / path).stat().st_mtime)
    except OSError:
        return 0


templates.env.globals["asset_v"] = _asset_v


async def _theme(session: AsyncSession) -> str:
    """Active theme name, used as the data-theme attribute on <html>."""
    return (await load_settings(session))["theme"]


def _parse_cursor(raw: str) -> tuple[datetime, int] | None:
    """'2026-07-01T10:00:00+00:00_123' -> (datetime, 123); None when absent or malformed."""
    stamp, _, row_id = raw.rpartition("_")
    try:
        parsed = datetime.fromisoformat(stamp)
        return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)), int(row_id)
    except ValueError:
        return None


@router.get("/", response_class=HTMLResponse)
async def home(
    request: Request,
    q: str = Query(""),
    cat: str = Query(""),
    tree: str = Query(""),
    age: str = Query(""),
    minsize: float = Query(0),
    maxsize: float = Query(0),
    sort: str = Query(""),
    view: str = Query(""),
    page: int = Query(1),
    after: str = Query(""),
    session: AsyncSession = Depends(get_session),
) -> Response:
    # First-run: if no provider configured, send to the setup wizard.
    configured = await session.get(Setting, "provider_host")
    if configured is None:
        return RedirectResponse("/setup")

    prefs = await load_settings(session)
    sort = sort or prefs["default_sort"]
    view = view or prefs["default_view"]
    # Hiding adult content is Spotweb's ~cat0_z3 filter, applied to every listing.
    if prefs["hide_adult"]:
        if tree_targets_adult(tree):
            return RedirectResponse("/", status_code=303)
        # The empty tree ("All") short-circuited, leaving adult spots visible.
        tree = f"{tree},~cat0_z3" if tree else "cat0,cat1,cat2,cat3,~cat0_z3"
    cat_ids = [int(c) for c in cat.split(",") if c.strip().isdigit()]
    current = section_for(tree)
    section_label = next((s["label"] for s in FILTER_SECTIONS if s["key"] == current), "All")
    since = None
    if age:
        hours = {"24h": 24, "3d": 72, "1w": 168, "1m": 720}.get(age)
        if hours:
            now = datetime.now(UTC).replace(second=0, microsecond=0)
            since = now - timedelta(minutes=now.minute % 5, hours=hours)  # 5-min buckets: cacheable
    rows, total = await search_spots(
        session,
        query=q or None,
        categories=cat_ids or None,
        tree=tree or None,
        since=since,
        min_size=int(minsize * 1024**3) if minsize else None,
        max_size=int(maxsize * 1024**3) if maxsize else None,
        hide_failed=prefs["hide_failed_signatures"],
        sort=sort,
        offset=(max(page, 1) - 1) * prefs["per_page"],
        limit=prefs["per_page"],
        after=_parse_cursor(after),
    )
    last = rows[-1] if rows else None
    next_cursor = (
        f"{last.posted_at.isoformat()}_{last.id}"
        if last and sort in ("new", "old") and len(rows) == prefs["per_page"]
        else ""
    )
    return templates.TemplateResponse(
        request,
        "browse.html",
        {
            "spots": rows,
            "comment_counts": await comment_counts(session, [s.message_id for s in rows]),
            "downloaded": await downloaded_map(session, [s.message_id for s in rows]),
            "in_library": await library_marks(prefs, rows),
            "releases": await release_counts(session, rows, bool(prefs["hide_adult"])),
            "total": total,
            "q": q,
            "cat": cat,
            "tree": tree,
            "base_tree": base_tree(tree),
            "age": age,
            "minsize": minsize,
            "maxsize": maxsize,
            "sort": sort,
            "sections": visible_sections(prefs["hide_adult"]),
            "current_section": section_for(tree),
            "section_label": section_label,
            "counts": await section_counts(session),
            "dimensions": await option_counts(session, tree),
            # "All" has no single dimension set; offer them grouped by category.
            "groups": (
                await section_dimensions(session, prefs["hide_adult"])
                if not section_for(tree)
                else []
            ),
            "icons": RAIL_ICONS,
            "view": view if view in ("grid", "table") else "grid",
            "page": max(page, 1),
            "next_cursor": next_cursor,
            "cursor_mode": _parse_cursor(after) is not None,
            "per_page": prefs["per_page"],
            "pages": max(1, -(-total // prefs["per_page"])),
            "categories": CATEGORY_NAMES,
            "app_name": get_settings().app_name,
            "theme": await _theme(session),
        },
    )


async def _adult_and_hidden(session: AsyncSession, spot_id: int) -> bool:
    """True when this spot is adult and the instance is configured to hide it."""
    prefs = await load_settings(session)
    if not prefs["hide_adult"]:
        return False
    spot = await get_spot(session, spot_id)
    return spot is not None and is_adult_spot(spot)


@router.get("/image/{spot_id}")
async def spot_image(spot_id: int, session: AsyncSession = Depends(get_session)) -> Response:
    if await _adult_and_hidden(session, spot_id):
        return HTMLResponse("Not found", status_code=404)
    path = IMAGES_DIR / f"{spot_id}.jpg"
    if not path.exists():
        return HTMLResponse("Not found", status_code=404)
    return FileResponse(path, media_type="image/jpeg")


@router.get("/spot/{spot_id}", response_class=HTMLResponse)
async def detail(
    request: Request, spot_id: int, session: AsyncSession = Depends(get_session)
) -> HTMLResponse:
    spot = await get_spot(session, spot_id)
    if spot is None:
        return HTMLResponse("Not found", status_code=404)
    prefs = await load_settings(session)
    if prefs["hide_adult"] and is_adult_spot(spot):
        # Hidden means unreachable, not merely absent from listings: a direct
        # link, a search engine or a stale bookmark must not open it either.
        return HTMLResponse("Not found", status_code=404)
    if spot.enriched == 0:
        # Fetch the full spot (description/image/NZB) the first time it's viewed.
        try:
            await enrich_new_spots(session, images_dir=IMAGES_DIR, limit=1, only_id=spot_id)
            spot = (await get_spot(session, spot_id)) or spot
        except Exception:
            pass
    if prefs["retrieve_comments"]:
        try:  # bodies cost a request each, so they load on first view
            await fetch_comment_bodies(session, spot.message_id)
        except Exception:
            pass  # provider unreachable: show the headers we have
    comments = (
        (
            await session.execute(
                select(Comment)
                .where(Comment.spot_message_id == spot.message_id)
                .order_by(Comment.posted_at)
            )
        )
        .scalars()
        .all()
    )
    others = await siblings(session, spot, bool(prefs["hide_adult"]))
    api_user = (await session.execute(select(ApiUser))).scalars().first()
    return templates.TemplateResponse(
        request,
        "detail.html",
        {
            "spot": spot,
            "downloaded": (await downloaded_map(session, [spot.message_id])).get(spot.message_id),
            "spotter_status": await spotters.status_of(session, spot.spotter_id),
            "in_library": bool(await library_marks(prefs, [spot])),
            "others": others,
            "best_id": (dup_best([spot, *others]) or spot).id if others else None,
            "arr_kind": kind_for(spot),
            "arr_ready": arr_configured(prefs, kind_for(spot))
            and bool(spot.imdb_id or spot.tvmaze_id),
            "is_admin": await is_admin(session, request.cookies.get(COOKIE)),
            "comments": comments,
            "send_label": (
                {"sabnzbd": "Send to SABnzbd", "blackhole": "Save to black hole"}.get(
                    prefs["nzb_action"]
                )
                if await is_admin(session, request.cookies.get(COOKIE))
                else None
            ),
            "categories": CATEGORY_NAMES,
            "api_key": api_user.api_key if api_user else "",
            "app_name": get_settings().app_name,
            "theme": await _theme(session),
        },
    )


@router.post("/spot/{spot_id}/health")
async def spot_health(
    request: Request,
    spot_id: int,
    force: str = Form(""),
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    """Check whether the spot's articles are still on the news server (cached for a day).
    Forcing a fresh check is for administrators: it costs the provider real requests."""
    if force == "1" and not await is_admin(session, request.cookies.get(COOKIE)):
        force = ""
    spot = await get_spot(session, spot_id)
    prefs = await load_settings(session)
    if spot is None or (prefs["hide_adult"] and is_adult_spot(spot)):
        return JSONResponse({"ok": False, "message": "Not found"}, status_code=404)
    result = await check_spot(session, spot, force=force == "1")
    return JSONResponse({"ok": "error" not in result, **result})


@router.post("/spot/{spot_id}/send")
async def send_spot_route(
    request: Request,
    spot_id: int,
    force: str = Form(""),
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    """Send a spot's NZB to SABnzbd or the black-hole folder (admins only)."""
    if not await is_admin(session, request.cookies.get(COOKIE)):
        return JSONResponse({"ok": False, "message": "Administrators only"}, status_code=403)
    spot = await get_spot(session, spot_id)
    prefs = await load_settings(session)
    if spot is None or (prefs["hide_adult"] and is_adult_spot(spot)):
        return JSONResponse({"ok": False, "message": "Not found"}, status_code=404)
    if spot.enriched == 0:
        try:
            await enrich_new_spots(session, images_dir=IMAGES_DIR, limit=1, only_id=spot_id)
            spot = (await get_spot(session, spot_id)) or spot
        except Exception:
            pass
    result = await send_spot(session, spot, prefs, force=force == "1")
    return JSONResponse(result, status_code=200 if result["ok"] else 502)


def _watch_values(
    name: str, query: str, tree: str, min_gb: float, max_gb: float, action: str
) -> dict | None:
    name = re.sub(r"[\x00-\x1f]", "", name).strip()[:80]
    if not name or action not in watch_actions:
        return None
    return {
        "name": name,
        "query": re.sub(r"[\x00-\x1f]", "", query).strip()[:255],
        "tree": clean_tree(tree),
        "min_size": int(max(min_gb, 0) * 1024**3),
        "max_size": int(max(max_gb, 0) * 1024**3),
        "action": action,
    }


@router.get("/stats", response_class=HTMLResponse)
async def stats_page(
    request: Request, session: AsyncSession = Depends(get_session)
) -> HTMLResponse:
    from .config import SPOT_DATA_DIR

    prefs = await load_settings(session)
    hide = bool(prefs["hide_adult"])
    data = await cached(f"stats:{hide}", 300, lambda: stats_collect(session, hide))
    return templates.TemplateResponse(
        request,
        "stats.html",
        {
            "s": data,
            "spot_bars": stats_bars(data["spots_per_day"]),
            "download_bars": stats_bars(data["downloads_per_day"]),
            "db_size": _dir_size(SPOT_DATA_DIR),
            "app_name": get_settings().app_name,
            "theme": prefs["theme"],
        },
    )


@router.get("/watchlists", response_class=HTMLResponse)
async def watchlists_page(
    request: Request, session: AsyncSession = Depends(get_session)
) -> HTMLResponse:
    prefs = await load_settings(session)
    watches = (await session.execute(select(Watch).order_by(Watch.name))).scalars().all()
    recent: dict[int, list] = {}
    for w in watches:
        recent[w.id] = list(
            (
                await session.execute(
                    select(WatchHit)
                    .where(WatchHit.watch_id == w.id)
                    .order_by(WatchHit.id.desc())
                    .limit(3)
                )
            )
            .scalars()
            .all()
        )
    return templates.TemplateResponse(
        request,
        "watchlists.html",
        {
            "watches": watches,
            "recent": recent,
            "sections": [
                s for s in FILTER_SECTIONS if not (prefs["hide_adult"] and s["key"] == "erotica")
            ],
            "channels": notify_channels(prefs),
            "can_send": prefs["nzb_action"] in ("sabnzbd", "blackhole"),
            "app_name": get_settings().app_name,
            "theme": prefs["theme"],
        },
    )


@router.post("/watchlists")
async def watchlists_create(
    name: str = Form(""),
    query: str = Form(""),
    tree: str = Form(""),
    min_gb: float = Form(0),
    max_gb: float = Form(0),
    action: str = Form("notify"),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    values = _watch_values(name, query, tree, min_gb, max_gb, action)
    taken = (
        values
        and (await session.execute(select(Watch.id).where(Watch.name == values["name"]))).first()
    )
    if values and not taken:
        await new_watch(session, **values)
    return RedirectResponse("/watchlists", status_code=303)


@router.post("/watchlists/{watch_id}/toggle")
async def watchlists_toggle(
    watch_id: int, session: AsyncSession = Depends(get_session)
) -> RedirectResponse:
    row = await session.get(Watch, watch_id)
    if row is not None:
        row.enabled = 0 if row.enabled else 1
        await session.commit()
    return RedirectResponse("/watchlists", status_code=303)


@router.post("/watchlists/{watch_id}/delete")
async def watchlists_delete(
    watch_id: int, session: AsyncSession = Depends(get_session)
) -> RedirectResponse:
    row = await session.get(Watch, watch_id)
    if row is not None:
        await session.execute(delete(WatchHit).where(WatchHit.watch_id == watch_id))
        await session.delete(row)
        await session.commit()
    return RedirectResponse("/watchlists", status_code=303)


@router.get("/watchlists/preview")
async def watchlists_preview(
    q: str = "",
    tree: str = "",
    min_gb: float = 0,
    max_gb: float = 0,
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    """How many spots a watch with these rules would match today (it only acts on new ones)."""
    prefs = await load_settings(session)
    rows, total = await search_spots(
        session,
        query=q.strip()[:255] or None,
        tree=clean_tree(tree) or None,
        min_size=int(max(min_gb, 0) * 1024**3) or None,
        max_size=int(max(max_gb, 0) * 1024**3) or None,
        exclude_adult=bool(prefs["hide_adult"]),
        hide_failed=bool(prefs["hide_failed_signatures"]),
        limit=3,
    )
    return JSONResponse({"count": total, "titles": [r.title[:60] for r in rows]})


@router.post("/settings/notify/test")
async def notify_test(session: AsyncSession = Depends(get_session)) -> JSONResponse:
    """Send a test message to every configured channel (saved settings)."""
    prefs = await load_settings(session)
    if not notify_channels(prefs):
        return JSONResponse({"ok": False, "message": "No notification channel is configured"})
    results = await send_notification(
        prefs, "SpotwebNG test", "Notifications are working.", prefs["public_url"]
    )
    ok = all(r["ok"] for r in results.values())
    detail = ", ".join(f"{name}: {r['message']}" for name, r in results.items())
    return JSONResponse({"ok": ok, "message": detail})


@router.post("/settings/arr/test")
async def arr_test(
    kind: str = Form(...),
    url: str = Form(""),
    apikey: str = Form(""),
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    """Check Radarr/Sonarr with the form's values (blank key = the saved one) and list the
    quality profiles and root folders to choose from."""
    if kind not in ("radarr", "sonarr"):
        return JSONResponse({"ok": False, "message": "Unknown application"}, status_code=400)
    prefs = await load_settings(session)
    target = url.strip().rstrip("/")
    if not re.fullmatch(r"https?://[^\s]+", target):
        return JSONResponse({"ok": False, "message": "Enter an http(s):// address"})
    client = ArrClient(kind, target, apikey or prefs[f"{kind}_apikey"])
    try:
        status = await client.status()
        options = await client.options()
    except ArrError as exc:
        return JSONResponse({"ok": False, "message": str(exc)})
    return JSONResponse(
        {"ok": True, "message": f"Connected to {status['app']} {status['version']}", **options}
    )


@router.post("/spot/{spot_id}/library")
async def add_to_library(
    request: Request, spot_id: int, session: AsyncSession = Depends(get_session)
) -> JSONResponse:
    """Add the spot's title to Radarr (movies) or Sonarr (series). Administrators only."""
    if not await is_admin(session, request.cookies.get(COOKIE)):
        return JSONResponse({"ok": False, "message": "Administrators only"}, status_code=403)
    spot = await get_spot(session, spot_id)
    prefs = await load_settings(session)
    if spot is None or (prefs["hide_adult"] and is_adult_spot(spot)):
        return JSONResponse({"ok": False, "message": "Not found"}, status_code=404)
    kind = kind_for(spot)
    if not arr_configured(prefs, kind):
        return JSONResponse({"ok": False, "message": f"{kind.title()} is not set up (Settings)"})
    if not (spot.imdb_id or spot.tvmaze_id):
        return JSONResponse({"ok": False, "message": "This spot has no IMDb or TVmaze link"})
    if not prefs[f"{kind}_profile_id"] or not prefs[f"{kind}_root"]:
        return JSONResponse(
            {
                "ok": False,
                "message": f"Choose a quality profile and root folder for {kind.title()} first",
            }
        )
    try:
        message = await arr_client_for(prefs, kind).add(
            spot.imdb_id, spot.tvmaze_id, prefs[f"{kind}_profile_id"], prefs[f"{kind}_root"]
        )
    except ArrError as exc:
        return JSONResponse({"ok": False, "message": str(exc)})
    return JSONResponse({"ok": True, "message": message})


@router.post("/spotter/{spotter_id}/status")
async def spotter_status(
    request: Request,
    spotter_id: str,
    status: str = Form(...),
    note: str = Form(""),
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    """Block, trust or reset a poster (administrators only)."""
    if not await is_admin(session, request.cookies.get(COOKIE)):
        return JSONResponse({"ok": False, "message": "Administrators only"}, status_code=403)
    try:
        await spotters.set_status(session, spotter_id, status, note)
    except ValueError:
        return JSONResponse({"ok": False, "message": "Invalid poster or status"}, status_code=400)
    return JSONResponse({"ok": True, "message": f"Poster marked {status}"})


@router.post("/settings/sab/test")
async def sab_test(
    url: str = Form(""), apikey: str = Form(""), session: AsyncSession = Depends(get_session)
) -> JSONResponse:
    """Check SABnzbd with the form's values; a blank key means the saved one."""
    prefs = await load_settings(session)
    target = url.strip().rstrip("/")
    if not re.fullmatch(r"https?://[^\s]+", target):
        return JSONResponse({"ok": False, "message": "Enter an http(s):// URL"})
    return JSONResponse(await check_sabnzbd(target, apikey or prefs["sab_apikey"]))


@router.get("/setup", response_class=HTMLResponse)
async def setup_form(
    request: Request, session: AsyncSession = Depends(get_session)
) -> HTMLResponse:
    current = await public_settings(session)
    api_user = (await session.execute(select(ApiUser))).scalars().first()
    return templates.TemplateResponse(
        request,
        "setup.html",
        {
            "providers": load_providers(),
            "providers_json": json.dumps(load_providers()),
            "current": current,
            "api_key": api_user.api_key if api_user else get_settings().default_api_key,
            "has_account": await account_exists(session),
            "app_name": get_settings().app_name,
            "theme": await _theme(session),
        },
    )


_background: set[asyncio.Task] = set()  # keep references so tasks aren't garbage-collected


async def _full_history_run() -> None:
    try:
        async with RETRIEVE_LOCK, SessionLocal() as session:
            prefs = await load_settings(session)
            result = await retrieve_spots(
                session,
                max_backfill=backfill_plan(prefs)["max_backfill"],
                retrieve_all=True,
                min_posted=retention_cutoff(prefs),
                max_age_days=prefs["retrieve_days"],
            )
        logging.getLogger("spotwebng").info("full-history retrieval: %s", result)
    except Exception:
        logging.getLogger("spotwebng").exception("full-history retrieval failed")


@router.post("/retrieve")
async def retrieve_now(
    full: str = Form(""), session: AsyncSession = Depends(get_session)
) -> JSONResponse:
    """Manually fetch new spots. `full=1` walks back to the server's oldest
    article in the background (it can take hours); otherwise a quick refresh."""
    if RETRIEVE_LOCK.locked():
        return JSONResponse(
            {"ok": False, "message": "A retrieval is already running"}, status_code=409
        )
    prefs = await load_settings(session)
    if full == "1":
        task = asyncio.create_task(_full_history_run())
        _background.add(task)
        task.add_done_callback(_background.discard)
        return JSONResponse({"ok": True, "started": True, "added": 0})
    async with RETRIEVE_LOCK:
        try:
            result = await retrieve_spots(
                session, max_backfill=backfill_plan(prefs)["max_backfill"]
            )
        except ValueError as exc:
            return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)
        except Exception as exc:
            return JSONResponse(
                {"ok": False, "message": f"Retrieval failed: {exc}"}, status_code=502
            )
    return JSONResponse({"ok": True, **result})


async def _signature_stats(session: AsyncSession) -> dict:
    rows = (
        await session.execute(select(Spot.verified, func.count()).group_by(Spot.verified))
    ).all()
    tally = {status: n for status, n in rows}
    return {
        "verified": tally.get(1, 0),
        "failed": tally.get(2, 0),
        "unsigned": tally.get(3, 0),
        "unevaluated": tally.get(0, 0),
    }


@router.get("/retrieve/status")
async def retrieve_status(session: AsyncSession = Depends(get_session)) -> JSONResponse:
    """What is running now, plus what we know about the server's history."""
    status = await load_status(session)
    prefs = await load_settings(session)
    oldest = status.get("oldest_date")
    if prefs["retrieve_days"] and oldest:
        # What a full retrieval will actually reach: the later of the server's
        # oldest article and the day limit.
        limit = (datetime.now(UTC) - timedelta(days=prefs["retrieve_days"])).isoformat()
        oldest = max(oldest, limit)
    return JSONResponse(
        {
            "live": LIVE,
            "status": status,
            "history_percent": history_percent(status),
            "effective_oldest": oldest,
            "retrieve_days": prefs["retrieve_days"],
            "jobs": await job_status(session),
            "signatures": await cached("sig-stats", 60, lambda: _signature_stats(session)),
            "backups": backup_summary(),
            "version": VERSION,
        }
    )


@router.post("/settings/maintenance")
async def maintenance(
    action: str = Form(...), confirm: str = Form(""), session: AsyncSession = Depends(get_session)
) -> JSONResponse:
    """Clean up, reset or wipe the index. Destructive actions need confirm=DELETE."""
    if action not in ACTIONS:
        return JSONResponse({"ok": False, "message": "Unknown action"}, status_code=400)
    if action in DESTRUCTIVE and confirm != "DELETE":
        return JSONResponse({"ok": False, "message": "Type DELETE to confirm"}, status_code=400)
    if RETRIEVE_LOCK.locked():
        return JSONResponse(
            {"ok": False, "message": "A retrieval or import is running; try again later"},
            status_code=409,
        )
    async with RETRIEVE_LOCK:
        try:
            message = await run_maintenance(session, action)
        except Exception as exc:
            logging.getLogger("spotwebng").exception("maintenance %s failed", action)
            return JSONResponse(
                {"ok": False, "message": f"Failed ({type(exc).__name__}); see the server log"},
                status_code=500,
            )
    logging.getLogger("spotwebng").info("maintenance %s: %s", action, message)
    return JSONResponse({"ok": True, "message": message})


@router.get("/settings/export")
async def export_database(session: AsyncSession = Depends(get_session)) -> StreamingResponse:
    """Download the index (spots, comments, bookmarks) as a gzip'd JSON Lines file."""
    # The generator opens its own sessions: the request's one is closed by the time
    # a long stream finishes.
    factory = async_sessionmaker(session.bind, expire_on_commit=False)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M")
    return StreamingResponse(
        export_chunks(factory),
        media_type="application/gzip",
        headers={"Content-Disposition": f'attachment; filename="spotwebng-{stamp}.jsonl.gz"'},
    )


async def _import_run(factory, path: str) -> None:
    log = logging.getLogger("spotwebng")
    try:
        async with RETRIEVE_LOCK:
            live_set(running=True, phase="Importing database", detail="", percent=0.0)
            try:
                result = await import_file(
                    factory, path, on_progress=lambda pct, msg: live_set(percent=pct, detail=msg)
                )
                outcome = {"ok": True, **result}
                await jobs_rerun(factory, "verify-signatures")  # imported rows start unverified
            except ValueError as exc:
                outcome = {"ok": False, "error": str(exc)}
            except Exception:
                log.exception("database import failed")
                outcome = {"ok": False, "error": "Import failed; see the server log"}
            finally:
                live_idle()
            async with factory() as session:
                await save_status(
                    session, last_import={**outcome, "at": datetime.now(UTC).isoformat()}
                )
                await session.commit()
        log.info("database import: %s", outcome)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


@router.post("/settings/import")
async def import_database(
    file: UploadFile = File(...), session: AsyncSession = Depends(get_session)
) -> JSONResponse:
    """Merge an export file into this install, in the background."""
    if RETRIEVE_LOCK.locked():
        return JSONResponse({"ok": False, "message": "A retrieval is running; try later"}, 409)
    from .config import SPOT_DATA_DIR

    SPOT_DATA_DIR.mkdir(parents=True, exist_ok=True)
    fd, path = tempfile.mkstemp(prefix="import-", suffix=".gz", dir=SPOT_DATA_DIR)
    with os.fdopen(fd, "wb") as out:
        await asyncio.to_thread(shutil.copyfileobj, file.file, out)
    factory = async_sessionmaker(session.bind, expire_on_commit=False)
    task = asyncio.create_task(_import_run(factory, path))
    _background.add(task)
    task.add_done_callback(_background.discard)
    return JSONResponse({"ok": True, "started": True})


@router.post("/setup/apikey")
async def setup_apikey(session: AsyncSession = Depends(get_session)) -> JSONResponse:
    """Rotate the Newznab API key: revoke all existing keys, issue one new key."""
    key = secrets.token_hex(16)
    for user in (await session.execute(select(ApiUser))).scalars().all():
        await session.delete(user)
    session.add(ApiUser(name="default", api_key=key))
    await session.commit()
    return JSONResponse({"key": key})


@router.post("/setup/test")
async def setup_test(
    provider_host: str = Form(...),
    provider_port: int = Form(563),
    provider_ssl: str = Form("yes"),
    username: str = Form(""),
    password: str = Form(""),
) -> JSONResponse:
    result = await check_nntp(
        provider_host,
        provider_port,
        use_ssl=provider_ssl == "yes",
        username=username,
        password=password,
    )
    return JSONResponse(result)


@router.post("/setup")
async def setup_save(
    session: AsyncSession = Depends(get_session),
    account_username: str = Form(""),
    account_password: str = Form(""),
    provider_name: str = Form(""),
    provider_host: str = Form(...),
    provider_port: int = Form(563),
    provider_ssl: str = Form("yes"),
    username: str = Form(""),
    password: str = Form(""),
    connections: int = Form(4),
) -> RedirectResponse:
    # First run: create the login here, so a fresh install is never left open.
    # An existing account is never overwritten from this form.
    if account_password and not await account_exists(session):
        await create_user(session, account_username or "admin", account_password, ADMIN)

    # A blank password field means "unchanged" — the form never receives the
    # stored secret, so an empty submission must not wipe it.
    if not password:
        existing_pw = await session.get(Setting, "password")
        password = existing_pw.value if existing_pw else ""
    values = {
        "provider_name": provider_name,
        "provider_host": provider_host,
        "provider_port": str(provider_port),
        "provider_ssl": provider_ssl,
        "username": username,
        "password": password,
        "connections": str(connections),
    }
    for key, value in values.items():
        if key in SECRET_KEYS:
            value = encrypt(value)  # idempotent: a kept, already-encrypted value passes through
        existing = await session.get(Setting, key)
        if existing:
            existing.value = value
        else:
            session.add(Setting(key=key, value=value))
    await session.commit()
    return RedirectResponse("/", status_code=303)


# Feather-style icons for the browse rail, keyed by section.
RAIL_ICONS = {
    "": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M4 6h16M4 12h16M4 18h16"/></svg>',
    "movies": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"><rect x="2" y="4" width="20" height="16" rx="2"/><path d="M7 4v16M17 4v16M2 10h20"/></svg>',
    "series": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"><rect x="2" y="7" width="20" height="13" rx="2"/><path d="m8 3 4 4 4-4"/></svg>',
    "books": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"><path d="M4 19.5V5a2 2 0 0 1 2-2h13v18H6a2 2 0 0 1-2-2Z"/><path d="M8 7h7"/></svg>',
    "music": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"><circle cx="6" cy="17" r="3"/><circle cx="18" cy="15" r="3"/><path d="M9 17V5l12-2v12"/></svg>',
    "games": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><rect x="2" y="6" width="20" height="12" rx="4"/><path d="M6 12h4M8 10v4M15 13h.01M18 11h.01"/></svg>',
    "apps": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></svg>',
    "erotica": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"><path d="M19 14c1.5-1.5 3-3.5 3-5.5A4.5 4.5 0 0 0 12 6a4.5 4.5 0 0 0-10 2.5C2 12 7 16 12 20c2-1.6 4.4-3.6 7-6Z"/></svg>',
}


# ---------- authentication ----------


@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request, session: AsyncSession = Depends(get_session)) -> Response:
    if not await account_exists(session):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "app_name": get_settings().app_name,
            "error": "",
            "theme": await _theme(session),
        },
    )


@router.post("/login")
async def login_submit(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    session: AsyncSession = Depends(get_session),
):
    throttle_key = (request.client.host if request.client else "?") + "|" + username.strip().lower()
    wait = login_locked(throttle_key)
    if wait:
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "app_name": get_settings().app_name,
                "error": f"Too many failed attempts. Try again in {wait // 60 + 1} minute(s).",
                "theme": await _theme(session),
            },
            status_code=429,
        )
    user = await verify_credentials(session, username, password)
    if user is None:
        record_login_failure(throttle_key)
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "app_name": get_settings().app_name,
                "error": "Incorrect username or password.",
                "theme": await _theme(session),
            },
            status_code=200,
        )
    clear_login_failures(throttle_key)
    token = await start_session(session, user)
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        COOKIE,
        token,
        max_age=60 * 60 * 24 * SESSION_DAYS,
        httponly=True,
        samesite="lax",
        path="/",
    )
    return response


@router.post("/logout")
async def logout(request: Request, session: AsyncSession = Depends(get_session)):
    await end_session(session, request.cookies.get(COOKIE))
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(COOKIE, path="/")
    return response


# ---------- settings ----------


def _dir_size(path) -> str:
    try:
        total = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    except OSError:
        total = 0
    for unit in ("B", "KB", "MB", "GB"):
        if total < 1024 or unit == "GB":
            return f"{total:.0f} {unit}" if unit == "B" else f"{total:.1f} {unit}"
        total /= 1024
    return "0 B"


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(
    request: Request,
    saved: int = Query(0),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    from .config import SPOT_DATA_DIR

    current = await public_settings(session)
    spots_total = (await session.execute(select(func.count()).select_from(Spot))).scalar_one()
    images = (
        await session.execute(select(func.count()).select_from(Spot).where(Spot.poster_url != ""))
    ).scalar_one()
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "values": await load_settings(session),
            "current": current,
            "api_keys": (await session.execute(select(ApiUser))).scalars().all(),
            "has_account": await account_exists(session),
            "users": (await session.execute(select(User).order_by(User.username))).scalars().all(),
            "spotters": await spotters.listing(session),
            "me": await user_for_session(session, request.cookies.get(COOKIE)),
            "roles": ROLES,
            "stats": {
                "spots": spots_total,
                "images": images,
                "db_size": _dir_size(SPOT_DATA_DIR),
            },
            "base_url": str(request.base_url).rstrip("/"),
            "saved": bool(saved),
            "app_name": get_settings().app_name,
            "theme": await _theme(session),
        },
    )


@router.post("/settings")
async def settings_save(request: Request, session: AsyncSession = Depends(get_session)):
    form = dict(await request.form())
    await save_settings(session, {k: str(v) for k, v in form.items()})
    return RedirectResponse("/settings?saved=1", status_code=303)


@router.post("/settings/apikey")
async def settings_add_key(
    name: str = Form(...),
    scope: str = Form("download"),
    session: AsyncSession = Depends(get_session),
):
    # Anything but an explicit "full" gets the least-privileged key.
    session.add(
        ApiUser(
            name=name.strip()[:128] or "client",
            api_key=secrets.token_hex(16),
            scope="full" if scope == "full" else "download",
        )
    )
    await session.commit()
    return RedirectResponse("/settings#api", status_code=303)


@router.post("/settings/apikey/delete")
async def settings_delete_key(
    key_id: int = Form(...), session: AsyncSession = Depends(get_session)
):
    row = await session.get(ApiUser, key_id)
    if row is not None:
        await session.delete(row)
        await session.commit()
    return RedirectResponse("/settings#api", status_code=303)


@router.post("/settings/account")
async def settings_account(
    request: Request,
    current_password: str = Form(""),
    password: str = Form(""),
    session: AsyncSession = Depends(get_session),
):
    """Change your own password. Requires the current one, and signs out
    every session so a stolen cookie cannot outlive the change."""
    me = await user_for_session(session, request.cookies.get(COOKIE))
    if me is None:
        return RedirectResponse("/login", status_code=303)
    if password:
        if await verify_credentials(session, me.username, current_password) is None:
            return RedirectResponse("/settings?err=password#account", status_code=303)
        await set_password(session, me, password)
        await revoke_all_sessions(session)
    return RedirectResponse("/settings?saved=1#account", status_code=303)


@router.post("/settings/users")
async def settings_add_user(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    role: str = Form(VIEWER),
    session: AsyncSession = Depends(get_session),
):
    if not await is_admin(session, request.cookies.get(COOKIE)):
        return RedirectResponse("/", status_code=303)
    try:
        await create_user(session, username, password, role if role in ROLES else VIEWER)
    except ValueError:
        return RedirectResponse("/settings?err=user#users", status_code=303)
    return RedirectResponse("/settings?saved=1#users", status_code=303)


@router.post("/settings/users/delete")
async def settings_delete_user(
    request: Request,
    user_id: int = Form(...),
    session: AsyncSession = Depends(get_session),
):
    if not await is_admin(session, request.cookies.get(COOKIE)):
        return RedirectResponse("/", status_code=303)
    target = await session.get(User, user_id)
    # Removing the last admin would lock everyone out of Settings for good.
    if target is None or (target.role == ADMIN and await count_admins(session) <= 1):
        return RedirectResponse("/settings?err=lastadmin#users", status_code=303)
    await revoke_all_sessions(session, user_id=target.id)
    await session.delete(target)
    await session.commit()
    return RedirectResponse("/settings?saved=1#users", status_code=303)


@router.post("/settings/users/role")
async def settings_set_role(
    request: Request,
    user_id: int = Form(...),
    role: str = Form(VIEWER),
    session: AsyncSession = Depends(get_session),
):
    if not await is_admin(session, request.cookies.get(COOKIE)):
        return RedirectResponse("/", status_code=303)
    target = await session.get(User, user_id)
    if target is None:
        return RedirectResponse("/settings#users", status_code=303)
    if target.role == ADMIN and role != ADMIN and await count_admins(session) <= 1:
        return RedirectResponse("/settings?err=lastadmin#users", status_code=303)
    target.role = role if role in ROLES else VIEWER
    await session.commit()
    return RedirectResponse("/settings?saved=1#users", status_code=303)


@router.post("/settings/users/password")
async def settings_reset_user_password(
    request: Request,
    user_id: int = Form(...),
    password: str = Form(...),
    session: AsyncSession = Depends(get_session),
):
    if not await is_admin(session, request.cookies.get(COOKIE)):
        return RedirectResponse("/", status_code=303)
    target = await session.get(User, user_id)
    if target is not None:
        await set_password(session, target, password)
        await revoke_all_sessions(session, user_id=target.id)
    return RedirectResponse("/settings?saved=1#users", status_code=303)


@router.post("/settings/test")
async def settings_test(session: AsyncSession = Depends(get_session)) -> JSONResponse:
    """Test the saved provider credentials without reopening the wizard."""
    stored = await all_settings(session)
    if not stored.get("provider_host"):
        return JSONResponse({"ok": False, "message": "No provider configured yet"})
    result = await check_nntp(
        stored["provider_host"],
        int(stored.get("provider_port", 563) or 563),
        use_ssl=stored.get("provider_ssl", "yes") == "yes",
        username=stored.get("username", ""),
        password=stored.get("password", ""),
    )
    return JSONResponse(result)
