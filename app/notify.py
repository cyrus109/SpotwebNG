"""Notifications: generic webhook, Discord, ntfy and Telegram.

All targets are admin-configured and stored encrypted (they usually embed a token).
Nothing here puts a URL or token into a log line or an error message shown to a user.
"""

from __future__ import annotations

import re
from typing import Any

import httpx

TIMEOUT = 10.0
MAX_TEXT = 1500
_CTRL = re.compile(r"[\x00-\x1f\x7f]")


def _clean(text: str, limit: int) -> str:
    return text[:limit]


def _header(text: str) -> str:
    """HTTP header values: single line, latin-1 safe."""
    return _CTRL.sub(" ", text).encode("latin-1", "replace").decode("latin-1")[:200]


def channels(prefs: dict[str, Any]) -> list[str]:
    out = []
    if prefs.get("notify_webhook_url"):
        out.append("webhook")
    if prefs.get("notify_discord_url"):
        out.append("discord")
    if prefs.get("notify_ntfy_url"):
        out.append("ntfy")
    if prefs.get("notify_telegram_token") and prefs.get("notify_telegram_chat"):
        out.append("telegram")
    return out


async def _post(client: httpx.AsyncClient, url: str, **kwargs: Any) -> dict[str, Any]:
    try:
        resp = await client.post(url, **kwargs)
    except httpx.HTTPError as exc:  # never str(exc): it embeds the URL
        return {"ok": False, "message": f"unreachable ({type(exc).__name__})"}
    if resp.status_code >= 400:
        return {"ok": False, "message": f"HTTP {resp.status_code}"}
    return {"ok": True, "message": "sent"}


async def notify(
    prefs: dict[str, Any], title: str, text: str, link: str = ""
) -> dict[str, dict[str, Any]]:
    """Send to every configured channel; returns {channel: {ok, message}}."""
    title, text, link = _clean(title, 200), _clean(text, MAX_TEXT), _clean(link, 500)
    results: dict[str, dict[str, Any]] = {}
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False) as client:
        if prefs.get("notify_webhook_url"):
            results["webhook"] = await _post(
                client,
                prefs["notify_webhook_url"],
                json={"title": title, "text": text, "url": link},
            )
        if prefs.get("notify_discord_url"):
            body = f"**{title}**\n{text}" + (f"\n{link}" if link else "")
            results["discord"] = await _post(
                client, prefs["notify_discord_url"], json={"content": body[:1900]}
            )
        if prefs.get("notify_ntfy_url"):
            headers = {"Title": _header(title)}
            if link.startswith(("http://", "https://")):
                headers["Click"] = _header(link)
            results["ntfy"] = await _post(
                client, prefs["notify_ntfy_url"], content=text.encode(), headers=headers
            )
        if prefs.get("notify_telegram_token") and prefs.get("notify_telegram_chat"):
            body = f"{title}\n{text}" + (f"\n{link}" if link else "")
            results["telegram"] = await _post(
                client,
                f"https://api.telegram.org/bot{prefs['notify_telegram_token']}/sendMessage",
                json={"chat_id": prefs["notify_telegram_chat"], "text": body[:4000]},
            )
    return results


class FailureTracker:
    """Alerts once when retrievals fail N times in a row, and once when they recover."""

    def __init__(self) -> None:
        self.count = 0
        self.alerted = False

    async def failed(self, prefs: dict[str, Any], error: str) -> None:
        self.count += 1
        after = prefs.get("notify_failure_after", 0)
        if after and self.count >= after and not self.alerted and channels(prefs):
            self.alerted = True
            await notify(
                prefs,
                "SpotwebNG: retrieval is failing",
                f"{self.count} runs in a row have failed ({error}). Check the server log.",
            )

    async def succeeded(self, prefs: dict[str, Any]) -> None:
        if self.alerted and channels(prefs):
            await notify(prefs, "SpotwebNG: retrieval recovered", "Retrieval is working again.")
        self.count, self.alerted = 0, False
