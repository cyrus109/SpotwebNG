"""CSRF protection for every state-changing request made with a session cookie.

The token is derived from the session cookie (HMAC with a fixed label), so it
needs no storage and an attacker's page, which can neither read the cookie nor
guess the token, cannot produce it. Pages carry it in a <meta> tag; ui.js adds
it to fetch() calls (header) and to POST forms (hidden field). Requests without
a session cookie carry no ambient credentials and are not checked, which also
keeps the sign-in form working. The Newznab /api is GET-only and uses API keys.
"""

from __future__ import annotations

import hashlib
import hmac

from fastapi import Form, Header, HTTPException, Request

from .auth import COOKIE

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_LABEL = b"spotwebng-csrf-v1"


def token_for(session_cookie: str) -> str:
    return hmac.new(_LABEL, session_cookie.encode(), hashlib.sha256).hexdigest()


def csrf_context(request: Request) -> dict[str, str]:
    """Jinja context: the token for this request's session, or '' when signed out."""
    cookie = request.cookies.get(COOKIE)
    return {"csrf_token": token_for(cookie) if cookie else ""}


async def verify_csrf(
    request: Request,
    x_csrf_token: str = Header(""),
    csrf_token: str = Form(""),
) -> None:
    if request.method in SAFE_METHODS:
        return
    cookie = request.cookies.get(COOKIE)
    if not cookie:
        return  # nothing ambient to ride on
    supplied = x_csrf_token or csrf_token
    if not hmac.compare_digest(supplied.encode(), token_for(cookie).encode()):
        raise HTTPException(status_code=403, detail="CSRF token missing or invalid")
