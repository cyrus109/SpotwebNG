"""SpotwebNG — standalone modern Spotnet indexer + Newznab API.

Boot order: create tables -> seed (demo spots + default API key) -> serve.
Exposes:
  /            modern browsing UI (Netflix-style grid + table toggle)
  /spot/{id}   detail page
  /setup       provider setup wizard
  /api?t=...   Newznab API (consumed by Radarr / Sonarr / Prowlarr)
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from .auth import (
    COOKIE,
    is_admin,
    is_authorised,
    migrate_legacy_account,
    sweep_expired_sessions,
)
from .autobackup import backup_loop
from .comments import retrieve_comments
from .config import BASE_DIR, IMAGES_DIR, get_settings
from .csrf import PRE_SESSION_COOKIE
from .db import SessionLocal, get_session, init_db
from .jobs import run_jobs
from .newznab import router as newznab_router
from .retention import prune_old_spots, retention_cutoff
from .retriever import RETRIEVE_LOCK, enrich_new_spots, retrieve_spots
from .seed import seed_if_empty
from .settings_store import backfill_plan, load_settings
from .version import VERSION
from .web import router as web_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
logger = logging.getLogger("spotwebng")

RETRIEVE_INTERVAL = 15 * 60  # seconds between automatic retrievals
ENRICH_INTERVAL = 20  # seconds between enrichment batches
ENRICH_BATCH = 50
ENRICH_STARTUP_DELAY = 10  # let the first retrieval get going


async def _retrieve_loop() -> None:
    """Fetch new spots on boot, then on the configured interval. Retention runs
    in the same loop so an unattended install cannot fill its disk."""
    while True:
        interval = RETRIEVE_INTERVAL
        try:
            async with SessionLocal() as session:
                prefs = await load_settings(session)
                interval = prefs["retrieve_interval"] * 60
                async with RETRIEVE_LOCK:
                    plan = backfill_plan(prefs)
                    result = await retrieve_spots(
                        session, **plan, min_posted=retention_cutoff(prefs)
                    )
                    logger.info("spot retrieval: %s", result)
                    if prefs["retrieve_comments"]:
                        # A comments failure must not mask (or abort) the spot run above.
                        try:
                            cresult = await retrieve_comments(
                                session,
                                group=prefs["comment_group"],
                                # comments are far fewer than spots, so an article
                                # window stays a sensible start even in days mode
                                max_backfill=500_000
                                if prefs["retrieve_backfill_unit"] == "days"
                                else plan["max_backfill"],
                                retrieve_all=prefs["retrieve_all"],
                                min_posted=retention_cutoff(prefs),
                                max_age_days=plan["max_age_days"],
                            )
                            logger.info("comment retrieval: %s", cresult)
                        except Exception:
                            logger.exception("comment retrieval failed")
                await sweep_expired_sessions(session)
                pruned = await prune_old_spots(session)
                if pruned["deleted"] or pruned["stripped"]:
                    logger.info("retention: %s", pruned)
        except ValueError:
            logger.info("spot retrieval skipped: provider not configured yet")
        except Exception:
            logger.exception("spot retrieval failed")
        await asyncio.sleep(interval)


async def _enrich_loop() -> None:
    """Continuously enrich pending spots (newest first) with full-XML data."""
    await asyncio.sleep(ENRICH_STARTUP_DELAY)
    while True:
        try:
            async with SessionLocal() as session:
                if not (await load_settings(session))["auto_enrich"]:
                    await asyncio.sleep(ENRICH_INTERVAL)
                    continue
                result = await enrich_new_spots(session, images_dir=IMAGES_DIR, limit=ENRICH_BATCH)
            if result["enriched"] or result["failed"]:
                logger.info("spot enrichment: %s", result)
                await asyncio.sleep(2)  # keep draining while there's a backlog
                continue
        except ValueError:
            pass  # provider not configured yet
        except Exception:
            logger.exception("spot enrichment failed")
        await asyncio.sleep(ENRICH_INTERVAL)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    async with SessionLocal() as session:
        await seed_if_empty(session)
        # Installs from before roles kept one login in `settings`.
        await migrate_legacy_account(session)
    background = [
        asyncio.create_task(_retrieve_loop()),
        asyncio.create_task(_enrich_loop()),
        asyncio.create_task(run_jobs(SessionLocal)),
        asyncio.create_task(backup_loop(SessionLocal)),
    ]
    yield
    for task in background:
        task.cancel()


app = FastAPI(
    title=get_settings().app_name,
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
app.include_router(newznab_router)
app.include_router(web_router)

# Paths that must stay reachable without a browser session: the Newznab API
# authenticates with its own key, and the rest are needed to log in at all.
OPEN_PREFIXES = ("/api", "/static", "/healthz", "/login")
# Configuration surfaces — a viewer has no business here.
ADMIN_PREFIXES = ("/settings", "/setup", "/retrieve")


def is_open_path(path: str) -> bool:
    """Whole-segment match — bare startswith would also open a future /apikeys."""
    return any(
        path == p or path.startswith(p + "/") or path.startswith(p + "?") for p in OPEN_PREFIXES
    )


@app.middleware("http")
async def require_login(request: Request, call_next):
    path = request.url.path
    if is_open_path(path):
        return await call_next(request)
    # Resolve the session the same way routes do, so test overrides apply here too.
    provider = app.dependency_overrides.get(get_session, get_session)
    agen = provider()
    session = await agen.__anext__()
    try:
        authorised = await is_authorised(session, request.cookies.get(COOKIE))
    finally:
        await agen.aclose()
    if not authorised:
        return RedirectResponse("/login", status_code=303)
    # Admin-only areas: viewers may browse, but not configure the instance.
    if any(path == p or path.startswith(p + "/") for p in ADMIN_PREFIXES):
        provider = app.dependency_overrides.get(get_session, get_session)
        agen = provider()
        session = await agen.__anext__()
        try:
            allowed = await is_admin(session, request.cookies.get(COOKIE))
        finally:
            await agen.aclose()
        if not allowed:
            return RedirectResponse("/", status_code=303)
    return await call_next(request)


@app.middleware("http")
async def csrf_cookie(request: Request, call_next):
    """Hand a first-time visitor the pre-session CSRF cookie (see csrf.py)."""
    response = await call_next(request)
    fresh = getattr(request.state, "csrf_new", None)
    if fresh:
        response.set_cookie(PRE_SESSION_COOKIE, fresh, httponly=True, samesite="lax")
    return response


@app.get("/healthz")
async def healthz() -> JSONResponse:
    """A health check that returns 200 without touching the database is a
    check that stays green through an outage."""
    try:
        async with SessionLocal() as session:
            await session.execute(text("SELECT 1"))
    except Exception:
        logger.exception("health check failed")
        return JSONResponse({"status": "error", "database": "unreachable"}, status_code=503)
    from . import migrate

    return JSONResponse(
        {
            "status": "ok",
            "database": "ok",
            "version": VERSION,
            "schema": migrate.SCHEMA_REVISION,
        }
    )
