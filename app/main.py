"""SpotwebNG — standalone modern Spotnet indexer + Newznab API.

Boot order: create tables -> seed (demo spots + default API key) -> serve.
Exposes:
  /            modern browsing UI (Netflix-style grid + table toggle)
  /spot/{id}   detail page
  /setup       provider setup wizard
  /api?t=...   Newznab API (consumed by Radarr / Sonarr / Prowlarr)
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .config import BASE_DIR, get_settings
from .db import SessionLocal, init_db
from .newznab import router as newznab_router
from .seed import seed_if_empty
from .web import router as web_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    async with SessionLocal() as session:
        await seed_if_empty(session)
    yield


app = FastAPI(title=get_settings().app_name, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
app.include_router(newznab_router)
app.include_router(web_router)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}
