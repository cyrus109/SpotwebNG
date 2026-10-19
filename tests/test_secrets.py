"""Secrets at rest: encryption, key handling, legacy upgrade, nothing leaks."""

import os
import stat

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import jobs, secrets_store
from app.models import Base, Setting
from app.retriever import _load_provider
from app.settings_store import SPEC, load_settings, save_settings


@pytest.fixture
async def factory():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def test_round_trip_is_idempotent_and_unreadable_without_the_key(monkeypatch):
    token = secrets_store.encrypt("hunter2")
    assert token.startswith("enc:") and "hunter2" not in token
    assert secrets_store.encrypt(token) == token  # never double-encrypts
    assert secrets_store.decrypt(token) == "hunter2"
    assert secrets_store.encrypt("") == "" and secrets_store.decrypt("legacy") == "legacy"
    monkeypatch.setenv(secrets_store.KEY_ENV, Fernet.generate_key().decode())
    secrets_store.reset_key_cache()
    assert secrets_store.decrypt(token) == ""  # lost/changed key: empty, not a crash


def test_the_key_file_is_created_private_and_reused(tmp_path, monkeypatch):
    monkeypatch.delenv(secrets_store.KEY_ENV)
    monkeypatch.setattr("app.secrets_store.config.SPOT_DATA_DIR", tmp_path / "data")
    secrets_store.reset_key_cache()
    token = secrets_store.encrypt("x")
    key_file = tmp_path / "data" / "secret.key"
    assert stat.S_IMODE(os.stat(key_file).st_mode) == 0o600
    secrets_store.reset_key_cache()  # a restart reads the same key back
    assert secrets_store.decrypt(token) == "x"


def test_every_secret_kind_setting_is_listed_as_a_secret_key():
    declared = {k for k, (_d, kind) in SPEC.items() if kind == "secret"}
    assert declared <= secrets_store.SECRET_KEYS


async def test_saved_settings_are_encrypted_in_the_database_and_plain_in_use(factory):
    async with factory() as s:
        await save_settings(s, {"sab_apikey": "abc123key"})
        raw = (await s.get(Setting, "sab_apikey")).value
        assert raw.startswith("enc:") and "abc123key" not in raw
        assert (await load_settings(s))["sab_apikey"] == "abc123key"
        await save_settings(s, {"sab_apikey": ""})  # blank = unchanged
        assert (await load_settings(s))["sab_apikey"] == "abc123key"


async def test_legacy_plaintext_is_encrypted_by_the_job_and_still_works_meanwhile(factory):
    async with factory() as s:
        s.add(Setting(key="provider_host", value="news.example.com"))
        s.add(Setting(key="password", value="plain-legacy"))
        await s.commit()
        assert (await _load_provider(s))["password"] == "plain-legacy"  # before the job
    await jobs.run_jobs(factory, pause=0)
    async with factory() as s:
        raw = (await s.get(Setting, "password")).value
        assert raw.startswith("enc:") and "plain-legacy" not in raw
        assert (await _load_provider(s))["password"] == "plain-legacy"  # after
        assert (
            await s.execute(select(Setting).where(Setting.key == "provider_host"))
        ).scalar_one().value == "news.example.com"


async def test_the_settings_page_gets_only_a_presence_marker(factory):
    async with factory() as s:
        await save_settings(s, {"sab_apikey": "abc123key"})
        shown = await secrets_store.public_settings(s)
    assert shown["sab_apikey"] == "set" and "abc123key" not in str(shown)
