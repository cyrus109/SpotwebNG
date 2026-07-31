"""Accounts, sessions and roles.

Two roles: `admin` configures the instance (settings, users, provider);
`viewer` may only browse and search. Until the first user exists the app stays
open, so a fresh install can reach its own setup wizard — the wizard then
creates the administrator, which closes that window immediately.

The Newznab API is never cookie-gated: clients authenticate with an API key.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Session as SessionRow
from .models import Setting, User

COOKIE = "sw_session"
SESSION_DAYS = 30
_ITERATIONS = 240_000

ADMIN = "admin"
VIEWER = "viewer"
ROLES = (ADMIN, VIEWER)


def _hash(password: str, salt: bytes) -> str:
    derived = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _ITERATIONS)
    return f"pbkdf2${_ITERATIONS}${salt.hex()}${derived.hex()}"


def _matches(stored: str, password: str) -> bool:
    try:
        _, iterations, salt_hex, digest = stored.split("$")
    except ValueError:
        return False
    candidate = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), bytes.fromhex(salt_hex), int(iterations)
    ).hex()
    return hmac.compare_digest(digest, candidate)


# ---------- users ----------


def normalise(username: str) -> str:
    return username.strip().lower()


async def get_user(session: AsyncSession, username: str) -> User | None:
    result = await session.execute(select(User).where(User.username == normalise(username)))
    return result.scalar_one_or_none()


async def create_user(
    session: AsyncSession, username: str, password: str, role: str = VIEWER
) -> User:
    name = normalise(username)
    if not name:
        raise ValueError("A username is required")
    if await get_user(session, name):
        raise ValueError(f"User {name!r} already exists")
    user = User(
        username=name,
        display_name=username.strip(),
        password_hash=_hash(password, secrets.token_bytes(16)),
        role=role if role in ROLES else VIEWER,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def set_password(session: AsyncSession, user: User, password: str) -> None:
    user.password_hash = _hash(password, secrets.token_bytes(16))
    await session.commit()


async def verify_credentials(session: AsyncSession, username: str, password: str) -> User | None:
    user = await get_user(session, username)
    if user is None or not _matches(user.password_hash, password):
        return None
    return user


async def count_admins(session: AsyncSession) -> int:
    return (
        await session.execute(select(func.count()).select_from(User).where(User.role == ADMIN))
    ).scalar_one()


async def any_user_exists(session: AsyncSession) -> bool:
    return (await session.execute(select(func.count()).select_from(User))).scalar_one() > 0


# Kept for callers that only ask "is this install protected yet?".
account_exists = any_user_exists


async def migrate_legacy_account(session: AsyncSession) -> None:
    """Installs predating roles stored one login in `settings`. Move it across."""
    stored = await session.get(Setting, "auth_password")
    if stored is None or await any_user_exists(session):
        return
    name_row = await session.get(Setting, "auth_username")
    session.add(
        User(
            username=normalise(name_row.value if name_row else "admin"),
            display_name=(name_row.value if name_row else "admin"),
            password_hash=stored.value,
            role=ADMIN,
        )
    )
    for key in ("auth_password", "auth_username"):
        row = await session.get(Setting, key)
        if row is not None:
            await session.delete(row)
    await session.commit()


# ---------- sessions ----------


async def start_session(session: AsyncSession, user: User | None = None) -> str:
    token = secrets.token_urlsafe(32)
    session.add(
        SessionRow(
            token=token,
            user_id=user.id if user else None,
            expires_at=datetime.now(UTC) + timedelta(days=SESSION_DAYS),
        )
    )
    if user is not None:
        user.last_login = datetime.now(UTC)
    await session.commit()
    return token


async def user_for_session(session: AsyncSession, token: str | None) -> User | None:
    if not token:
        return None
    row = await session.get(SessionRow, token)
    if row is None:
        return None
    expires = row.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    if expires < datetime.now(UTC):
        await session.delete(row)
        await session.commit()
        return None
    return await session.get(User, row.user_id) if row.user_id else None


async def end_session(session: AsyncSession, token: str | None) -> None:
    if token:
        await session.execute(delete(SessionRow).where(SessionRow.token == token))
        await session.commit()


async def revoke_all_sessions(session: AsyncSession, user_id: int | None = None) -> None:
    stmt = delete(SessionRow)
    if user_id is not None:
        stmt = stmt.where(SessionRow.user_id == user_id)
    await session.execute(stmt)
    await session.commit()


async def sweep_expired_sessions(session: AsyncSession) -> int:
    """Delete stale rows. Without this the sessions table only ever grows."""
    result = await session.execute(
        delete(SessionRow).where(SessionRow.expires_at < datetime.now(UTC))
    )
    await session.commit()
    return int(getattr(result, "rowcount", 0) or 0)


async def is_authorised(session: AsyncSession, token: str | None) -> bool:
    """May this request proceed at all: no users yet, or a live session."""
    if not await any_user_exists(session):
        return True
    return await user_for_session(session, token) is not None


async def is_admin(session: AsyncSession, token: str | None) -> bool:
    """Admin-only areas; open only while the install has no users at all."""
    if not await any_user_exists(session):
        return True
    user = await user_for_session(session, token)
    return user is not None and user.role == ADMIN


# ---------- login throttling ----------
# In-process and deliberately simple: a single-instance app doesn't need a
# store, but an unthrottled login on a LAN is a free brute-force target.
MAX_ATTEMPTS = 5
LOCKOUT_SECONDS = 300
_attempts: dict[str, tuple[int, float]] = {}


def reset_login_throttle() -> None:
    _attempts.clear()


def login_locked(key: str) -> int:
    """Seconds remaining before `key` may try again (0 = allowed)."""
    count, first_seen = _attempts.get(key, (0, 0.0))
    if count < MAX_ATTEMPTS:
        return 0
    elapsed = time.monotonic() - first_seen
    if elapsed >= LOCKOUT_SECONDS:
        _attempts.pop(key, None)
        return 0
    return int(LOCKOUT_SECONDS - elapsed)


def record_login_failure(key: str) -> None:
    count, first_seen = _attempts.get(key, (0, 0.0))
    if count == 0 or time.monotonic() - first_seen >= LOCKOUT_SECONDS:
        _attempts[key] = (1, time.monotonic())
    else:
        _attempts[key] = (count + 1, first_seen)


def clear_login_failures(key: str) -> None:
    _attempts.pop(key, None)
