"""Encryption of stored secrets (Usenet password, API keys, tokens).

Fernet (AES-128-CBC + HMAC) with a key that lives *outside the database*: either the
SPOTWEBNG_SECRET_KEY environment variable or <data>/secret.key (created 0600 on first
use). A copied database file, an SQL dump or a backup therefore no longer exposes the
credentials without that key. The key file sits in the same volume by default, so for
real separation set the environment variable and keep it out of the volume.

Values are stored as "enc:<token>". Anything without the prefix is treated as legacy
plaintext (and encrypted by the `encrypt-secrets` job), so upgrades need no manual step.
A value that cannot be decrypted (lost key) reads as empty rather than crashing.
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from . import config
from .jobs import register
from .models import Setting

logger = logging.getLogger("spotwebng")

PREFIX = "enc:"
KEY_ENV = "SPOTWEBNG_SECRET_KEY"
# Setting keys whose values are secrets. (settings_store's "secret" kinds must be listed here.)
SECRET_KEYS = frozenset({"password", "sab_apikey"})


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    env = os.environ.get(KEY_ENV)
    if env:
        return Fernet(env.encode())
    path = config.SPOT_DATA_DIR / "secret.key"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return Fernet(path.read_bytes().strip())
    with os.fdopen(fd, "wb") as fh:
        fh.write(Fernet.generate_key())
    logger.info("created a new secret key at %s", path)
    return Fernet(path.read_bytes().strip())


def reset_key_cache() -> None:
    _fernet.cache_clear()


def is_encrypted(value: str) -> bool:
    return value.startswith(PREFIX)


def encrypt(value: str) -> str:
    """Idempotent: an already-encrypted value is returned unchanged; empty stays empty."""
    if not value or is_encrypted(value):
        return value
    return PREFIX + _fernet().encrypt(value.encode()).decode()


def decrypt(value: str) -> str:
    if not is_encrypted(value):
        return value  # legacy plaintext
    try:
        return _fernet().decrypt(value[len(PREFIX) :].encode()).decode()
    except InvalidToken:
        logger.warning("a stored secret cannot be decrypted (is the secret key the same one?)")
        return ""


async def all_settings(session: AsyncSession) -> dict[str, str]:
    """Every settings row as a dict, with secrets decrypted — for server-side use only."""
    rows = (await session.execute(select(Setting))).scalars().all()
    return {s.key: decrypt(s.value) if s.key in SECRET_KEYS else s.value for s in rows}


async def public_settings(session: AsyncSession) -> dict[str, str]:
    """Like all_settings but a secret becomes a presence marker ('set' or ''): safe for a template."""
    return {
        k: (("set" if v else "") if k in SECRET_KEYS else v)
        for k, v in (await all_settings(session)).items()
    }


@register("encrypt-secrets", 1, "Encrypting stored credentials")
async def _encrypt_legacy(session: AsyncSession) -> int:
    rows = (
        (await session.execute(select(Setting).where(Setting.key.in_(SECRET_KEYS)))).scalars().all()
    )
    changed = 0
    for row in rows:
        if row.value and not is_encrypted(row.value):
            row.value = encrypt(row.value)
            changed += 1
    await session.commit()
    return changed
