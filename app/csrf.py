"""CSRF protection for every state-changing request on the web UI.

The token is an HMAC of a cookie the attacker's page can neither read nor
(cross-site) send: the session cookie once signed in, otherwise a random
`sw_csrf` cookie handed out with the first page (a double-submit token, which
protects sign-in and the open first-run setup). Pages carry the token in a
<meta> tag; ui.js adds it to fetch() calls (header) and POST forms (field). A
request with neither cookie is refused. The Newznab /api is GET-only and uses
API keys instead.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

from fastapi import Form, Header, HTTPException, Request

from .auth import COOKIE

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
PRE_SESSION_COOKIE = "sw_csrf"
_LABEL = b"spotwebng-csrf-v1"


def token_for(cookie_value: str) -> str:
    return hmac.new(_LABEL, cookie_value.encode(), hashlib.sha256).hexdigest()


def _secret(request: Request) -> str | None:
    return request.cookies.get(COOKIE) or request.cookies.get(PRE_SESSION_COOKIE)


def csrf_context(request: Request) -> dict[str, str]:
    """Jinja context: this request's token. A visitor with no cookie yet gets a
    fresh pre-session secret, which the cookie middleware then sets."""
    secret = _secret(request)
    if secret is None:
        secret = secrets.token_urlsafe(32)
        request.state.csrf_new = secret
    return {"csrf_token": token_for(secret)}


async def verify_csrf(
    request: Request,
    x_csrf_token: str = Header(""),
    csrf_token: str = Form(""),
) -> None:
    if request.method in SAFE_METHODS:
        return
    secret = _secret(request)
    supplied = x_csrf_token or csrf_token
    if secret is None or not hmac.compare_digest(supplied.encode(), token_for(secret).encode()):
        raise HTTPException(status_code=403, detail="CSRF token missing or invalid")
