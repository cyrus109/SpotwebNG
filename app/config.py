"""Application configuration.

SpotwebNG is a standalone app. It talks to the outside world over HTTP only
(the Newznab API + its own web UI). Database defaults to a local SQLite file so
the app boots instantly for development; point DATABASE_URL at Postgres for
production.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SPOTWEBNEXT_", env_file=".env", extra="ignore")

    # SQLite for instant local dev; e.g. postgresql+asyncpg://user:pw@host/db for prod.
    database_url: str = f"sqlite+aiosqlite:///{BASE_DIR.parent}/spotwebnext.db"

    app_name: str = "SpotwebNG"
    # Seed a handful of demo spots on first boot so the UI/API are alive without NNTP.
    seed_demo_data: bool = True

    # Default API key handed to the first user; overridable via env in production.
    default_api_key: str = "changeme-spotwebnext-key"


@lru_cache
def get_settings() -> Settings:
    return Settings()


@lru_cache
def load_providers() -> list[dict]:
    """The 68-entry Usenet provider catalog, ported from Spotweb's usenetservers.xml."""
    with open(DATA_DIR / "providers.json", encoding="utf-8") as fh:
        return json.load(fh)
