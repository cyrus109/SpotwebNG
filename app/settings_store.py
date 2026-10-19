"""Typed application settings, stored as key/value rows in `settings`.

The setup wizard writes provider credentials into the same table; this module
owns the rest — retention, display, and behaviour — with defaults, coercion and
clamping so a bad value in the database can never break a page render.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Setting
from .secrets_store import decrypt, encrypt

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

# key -> (default, kind). kind drives coercion and form handling.
SPEC: dict[str, tuple[Any, str]] = {
    # retrieval
    "retrieve_interval": (15, "int"),  # minutes between automatic runs
    "retrieve_backfill": (20000, "int"),  # first run: this many articles or days (see unit)
    "retrieve_backfill_unit": ("articles", "str"),
    "retrieve_comments": (True, "bool"),  # index comment headers (bodies load on view)
    "comment_group": ("free.usenet", "str"),  # newsgroup Spotnet comments are posted to
    "retrieve_all": (False, "bool"),  # walk back to the server's oldest article
    "retrieve_days": (0, "int"),  # ...or only this many days back (0 = no limit)
    "auto_enrich": (True, "bool"),
    "enrich_older_than_days": (0, "int"),  # 0 = enrich everything; else only spots this young
    "backup_keep": (
        3,
        "int",
    ),  # automatic daily backups to keep (0 = off)  # fetch images/NZB refs in the background
    # retention
    "retention_days": (0, "int"),  # 0 = keep forever
    "retention_type": ("fullonly", "str"),  # fullonly | everything
    # sending a spot's NZB to a downloader
    "nzb_action": ("download", "str"),  # download | sabnzbd | blackhole
    "send_health_policy": ("block-dead", "str"),  # off | warn | block-dead
    # notifications (URLs can embed tokens, so they are secrets)
    "public_url": ("", "str"),  # this app's address, for links in notifications
    "notify_webhook_url": ("", "secret"),
    "notify_discord_url": ("", "secret"),
    "notify_ntfy_url": ("", "secret"),
    "notify_telegram_token": ("", "secret"),
    "notify_telegram_chat": ("", "str"),
    "notify_failure_after": (
        3,
        "int",
    ),  # alert after this many failed retrievals in a row (0 = off)
    "radarr_url": ("", "str"),
    "radarr_apikey": ("", "secret"),
    "radarr_profile_id": (0, "int"),
    "radarr_root": ("", "str"),
    "sonarr_url": ("", "str"),
    "sonarr_apikey": ("", "secret"),
    "sonarr_profile_id": (0, "int"),
    "sonarr_root": ("", "str"),
    "sab_url": ("", "str"),
    "sab_apikey": ("", "secret"),  # never rendered back into a page
    "sab_category": ("", "str"),
    "sab_priority": (-100, "int"),  # SABnzbd: -100 category default .. 2 force
    "blackhole_dir": ("", "str"),
    # display
    "per_page": (120, "int"),
    "default_sort": ("new", "str"),
    "default_view": ("grid", "str"),
    # Safe by default: an unconfigured instance must never render adult
    # covers, and only an admin can turn this off.
    "hide_adult": (True, "bool"),
    "hide_failed_signatures": (False, "bool"),  # hide spots whose signature did not verify
    "site_name": ("SpotwebNG", "str"),
    "theme": ("liquid-glass", "str"),
}

DEFAULTS: dict[str, Any] = {key: default for key, (default, _) in SPEC.items()}

CLAMPS = {
    "per_page": (12, 500),
    "retrieve_interval": (5, 1440),
    "retrieve_backfill": (1, 100_000_000),
    "retention_days": (0, 36500),
    "retrieve_days": (0, 36500),
    "backup_keep": (0, 60),
    "notify_failure_after": (0, 50),
    "radarr_profile_id": (0, 10_000),
    "sonarr_profile_id": (0, 10_000),
    "enrich_older_than_days": (0, 36500),
    "sab_priority": (-100, 2),
}

CHOICES = {
    "retrieve_backfill_unit": ("articles", "days"),
    "nzb_action": ("download", "sabnzbd", "blackhole"),
    "send_health_policy": ("off", "warn", "block-dead"),
    "retention_type": ("fullonly", "everything"),
    "default_sort": ("new", "old", "size", "title"),
    "default_view": ("grid", "table"),
    "theme": ("liquid-glass", "midnight"),
}


def _coerce(key: str, raw: str) -> Any:
    default, kind = SPEC[key]
    if kind == "int":
        try:
            value = int(str(raw).strip())
        except (TypeError, ValueError):
            return default
        bounds = CLAMPS.get(key)
        if bounds is not None:
            low, high = bounds
            value = max(low, min(high, value))
        return value
    if kind == "bool":
        return str(raw).strip().lower() in ("1", "true", "on", "yes")
    text = _CONTROL.sub("", str(raw)).strip()
    if key in (
        "sab_url",
        "public_url",
        "radarr_url",
        "sonarr_url",
    ):  # an http(s) base URL, or nothing
        return text.rstrip("/") if re.fullmatch(r"https?://[^\s]+", text) else default
    if key in ("notify_webhook_url", "notify_discord_url", "notify_ntfy_url"):
        return text if (not text or re.fullmatch(r"https?://[^\s]+", text)) else default
    if key == "comment_group" and not re.match(r"^[A-Za-z0-9._+-]{1,128}$", text):
        return default  # it ends up in an NNTP command line
    allowed = CHOICES.get(key)
    if allowed and text not in allowed:
        return default
    return text or default


async def load_settings(session: AsyncSession) -> dict[str, Any]:
    """All known settings, with defaults filled in and values coerced."""
    stored = {s.key: s.value for s in (await session.execute(select(Setting))).scalars().all()}
    return {
        key: _coerce(key, decrypt(stored[key]) if SPEC[key][1] == "secret" else stored[key])
        if key in stored
        else default
        for key, default in DEFAULTS.items()
    }


def backfill_plan(prefs: dict[str, Any]) -> dict[str, Any]:
    """Turn the backfill / history settings into retrieve_spots arguments.

    In *days* mode the first run is "walk back N days", which is the history
    walk with a day limit; the article window then only seeds the starting chunk.
    """
    full, limit = prefs["retrieve_all"], prefs["retrieve_days"]
    if prefs["retrieve_backfill_unit"] == "days" and not full:
        return {
            "max_backfill": 5000,
            "retrieve_all": True,
            "max_age_days": min(prefs["retrieve_backfill"], 36500),
        }
    return {
        "max_backfill": max(prefs["retrieve_backfill"], 1000),
        "retrieve_all": full,
        "max_age_days": limit if full else 0,
    }


async def get_setting(session: AsyncSession, key: str) -> Any:
    return (await load_settings(session)).get(key)


async def save_settings(session: AsyncSession, values: dict[str, str]) -> dict[str, Any]:
    """Persist the known keys from a form submission; unknown keys are ignored.

    Booleans absent from the payload are only cleared when the caller includes
    the matching `<key>__present` marker, so partial forms stay safe.
    """
    for key, (_default, kind) in SPEC.items():
        present = key in values
        if kind == "bool" and f"{key}__present" in values:
            raw = "on" if present else ""
        elif present and kind == "secret" and not values[key]:
            continue  # a blank secret field means "unchanged": it is never sent back
        elif present:
            raw = values[key]
        else:
            continue
        existing = await session.get(Setting, key)
        text = str(_coerce(key, raw))
        if kind == "secret":
            if not text:
                continue  # blank or rejected input never wipes a stored secret
            text = encrypt(text)
        if existing:
            existing.value = text
        else:
            session.add(Setting(key=key, value=text))
    await session.commit()
    return await load_settings(session)
