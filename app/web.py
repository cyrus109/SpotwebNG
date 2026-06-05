"""Web UI routes — the modern, Netflix-style browsing experience + setup wizard."""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .categories import CATEGORY_NAMES
from .config import BASE_DIR, get_settings, load_providers
from .db import get_session
from .models import Setting
from .spots import get_spot, search_spots

router = APIRouter()
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def _gb(size: int) -> str:
    return f"{size / 1024**3:.1f} GB" if size else "—"


templates.env.filters["gb"] = _gb
templates.env.filters["catname"] = lambda c: CATEGORY_NAMES.get(c, "Other")


async def _setting(session: AsyncSession, key: str) -> str | None:
    return await session.get(Setting, key) and (await session.get(Setting, key)).value


@router.get("/", response_class=HTMLResponse)
async def home(
    request: Request,
    q: str = Query(""),
    cat: int | None = Query(None),
    view: str = Query("grid"),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    # First-run: if no provider configured, send to the setup wizard.
    configured = await session.get(Setting, "provider_host")
    if configured is None:
        return RedirectResponse("/setup")

    rows, total = await search_spots(
        session, query=q or None, categories=[cat] if cat else None, limit=100
    )
    return templates.TemplateResponse(
        request,
        "browse.html",
        {
            "spots": rows,
            "total": total,
            "q": q,
            "cat": cat,
            "view": view if view in ("grid", "table") else "grid",
            "categories": CATEGORY_NAMES,
            "app_name": get_settings().app_name,
        },
    )


@router.get("/spot/{spot_id}", response_class=HTMLResponse)
async def detail(request: Request, spot_id: int, session: AsyncSession = Depends(get_session)) -> HTMLResponse:
    spot = await get_spot(session, spot_id)
    if spot is None:
        return HTMLResponse("Not found", status_code=404)
    return templates.TemplateResponse(
        request,
        "detail.html",
        {"spot": spot, "categories": CATEGORY_NAMES, "app_name": get_settings().app_name},
    )


@router.get("/setup", response_class=HTMLResponse)
async def setup_form(request: Request, session: AsyncSession = Depends(get_session)) -> HTMLResponse:
    current = {s.key: s.value for s in (await session.execute(select(Setting))).scalars().all()}
    return templates.TemplateResponse(
        request,
        "setup.html",
        {
            "providers": load_providers(),
            "providers_json": json.dumps(load_providers()),
            "current": current,
            "default_api_key": get_settings().default_api_key,
            "app_name": get_settings().app_name,
        },
    )


@router.post("/setup")
async def setup_save(
    session: AsyncSession = Depends(get_session),
    provider_name: str = Form(""),
    provider_host: str = Form(...),
    provider_port: int = Form(563),
    provider_ssl: str = Form("yes"),
    username: str = Form(""),
    password: str = Form(""),
    connections: int = Form(4),
) -> RedirectResponse:
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
        existing = await session.get(Setting, key)
        if existing:
            existing.value = value
        else:
            session.add(Setting(key=key, value=value))
    await session.commit()
    return RedirectResponse("/", status_code=303)
