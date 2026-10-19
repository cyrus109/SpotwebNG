"""Schema upgrades: fresh install, existing install, old schema, safety backup."""

import sqlite3
import tomllib
from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

from app import migrate
from app.models import Base
from app.version import VERSION


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(migrate, "SPOT_DATA_DIR", tmp_path / "data")
    return tmp_path / "data"


def _engine(tmp_path):
    return create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/t.db")


async def _schema_diff(engine):
    def run(conn):
        ctx = MigrationContext.configure(
            conn,
            opts={
                "compare_type": True,
                "include_object": lambda obj, name, type_, reflected, compare_to: (
                    not (type_ == "table" and name.startswith("spots_fts"))
                ),
            },
        )
        return compare_metadata(ctx, Base.metadata)

    async with engine.connect() as conn:
        return await conn.run_sync(run)


async def test_a_fresh_database_gets_exactly_the_models_schema(tmp_path, data_dir):
    engine = _engine(tmp_path)
    try:
        result = await migrate.upgrade_database(engine)
        assert result["from"] is None and result["to"] == migrate.head_revision()
        assert await _schema_diff(engine) == []  # migrations and models cannot drift apart
        assert result["backup"] is None  # nothing to protect yet
        again = await migrate.upgrade_database(engine)
        assert again["from"] == again["to"]  # second start is a no-op
    finally:
        await engine.dispose()


async def _install_with_old_schema(engine):
    """A pre-0.3 database: the baseline schema with no alembic_version table, plus a spot."""
    await migrate.upgrade_database(engine, target="0001")
    async with engine.begin() as conn:
        await conn.execute(text("DROP TABLE alembic_version"))
        await conn.execute(
            text(
                "INSERT INTO spots (message_id, title, poster, category, size, posted_at,"
                " subcat_a, subcat_b, subcat_c, subcat_d, subcat_z, spot_category, imdb_id,"
                " tvmaze_id, poster_url, description, nzb_ref, enriched, website, nzb_segments)"
                " VALUES ('<a@x>', 'Kept', '', 0, 0, '2026-01-01', '', '', '', '', '', -1, '',"
                " '', '', '', '', 1, '', '')"
            )
        )


async def test_an_install_from_before_migrations_keeps_its_data_and_is_backed_up(
    tmp_path, data_dir
):
    engine = _engine(tmp_path)
    try:
        await _install_with_old_schema(engine)
        result = await migrate.upgrade_database(engine)
        assert result["from"] is None and result["to"] == migrate.head_revision()
        backup = Path(result["backup"])
        assert backup.parent == data_dir / "backups" and backup.exists()
        con = sqlite3.connect(backup)  # the safety copy is the pre-upgrade database
        assert con.execute("SELECT title FROM spots").fetchone() == ("Kept",)
        assert "from_hdr" not in {r[1] for r in con.execute("PRAGMA table_info(spots)")}
        con.close()
        async with engine.connect() as conn:
            row = (
                await conn.execute(text("SELECT title, verified, keyid, from_hdr FROM spots"))
            ).one()
        assert tuple(row) == ("Kept", 0, -1, "")  # old rows get neutral defaults for new columns
        assert await _schema_diff(engine) == []
        assert len(list((data_dir / "backups").glob("pre-migrate-*"))) == 1
        await migrate.upgrade_database(engine)  # already current: no second backup
        assert len(list((data_dir / "backups").glob("pre-migrate-*"))) == 1
    finally:
        await engine.dispose()


async def test_each_new_revision_upgrades_an_existing_database_in_place(tmp_path, data_dir):
    engine = _engine(tmp_path)
    try:
        await migrate.upgrade_database(engine, target="0002")
        async with engine.begin() as conn:
            await conn.execute(text("INSERT INTO settings VALUES ('theme', 'midnight')"))
            await conn.execute(
                text(
                    "INSERT INTO spots (message_id, title, poster, category, size, posted_at,"
                    " subcat_a, subcat_b, subcat_c, subcat_d, subcat_z, spot_category, imdb_id,"
                    " tvmaze_id, poster_url, description, nzb_ref, enriched, website, nzb_segments)"
                    " VALUES ('<b@x>', 'Old', '', 0, 0, '2026-01-01', '', '', '', '', '', -1, '',"
                    " '', '', '', '', 1, '', '')"
                )
            )
        result = await migrate.upgrade_database(engine)
        assert result["from"] == "0002" and result["backup"]
        async with engine.connect() as conn:
            assert (await conn.execute(text("SELECT title FROM spots"))).scalar_one() == "Old"
            assert (
                await conn.execute(text("SELECT value FROM settings"))
            ).scalar_one() == "midnight"
        assert await _schema_diff(engine) == []
    finally:
        await engine.dispose()


async def test_missing_columns_on_an_old_schema_are_added_without_losing_rows(tmp_path, data_dir):
    engine = _engine(tmp_path)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("CREATE TABLE settings (key VARCHAR(128) PRIMARY KEY, value TEXT NOT NULL)")
            )
            await conn.execute(text("INSERT INTO settings VALUES ('theme', 'midnight')"))
            await conn.execute(
                text("CREATE TABLE spots (id INTEGER PRIMARY KEY, message_id VARCHAR(255))")
            )
        await migrate.upgrade_database(engine)
        async with engine.connect() as conn:
            cols = {
                c["name"] for c in await conn.run_sync(lambda c: inspect(c).get_columns("spots"))
            }
            assert {"title", "posted_at", "website", "nzb_segments"} <= cols
            assert (
                await conn.execute(text("SELECT value FROM settings"))
            ).scalar_one() == "midnight"
    finally:
        await engine.dispose()


def test_the_version_constant_matches_pyproject():
    pyproject = tomllib.loads((Path(__file__).parent.parent / "pyproject.toml").read_text())
    assert pyproject["project"]["version"] == VERSION


async def test_healthz_reports_version_and_schema():
    import httpx

    from app.main import app

    migrate.SCHEMA_REVISION = "0002"
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        body = (await c.get("/healthz")).json()
    assert body["version"] == VERSION and body["schema"] == "0002"


async def test_adding_columns_never_drops_the_search_index_triggers(tmp_path, data_dir):
    """SQLite batch mode rebuilds the table and takes its triggers with it; ours must not."""
    engine = _engine(tmp_path)
    try:
        await migrate.upgrade_database(engine, target="0004")  # FTS table + insert trigger exist
        async with engine.connect() as conn:
            before = {
                r[0]
                for r in await conn.execute(
                    text("SELECT name FROM sqlite_master WHERE type='trigger'")
                )
            }
        assert "spots_fts_ai" in before
        await migrate.upgrade_database(engine)  # later revisions add columns to spots
        async with engine.connect() as conn:
            after = {
                r[0]
                for r in await conn.execute(
                    text("SELECT name FROM sqlite_master WHERE type='trigger'")
                )
            }
            cols = {
                c["name"] for c in await conn.run_sync(lambda c: inspect(c).get_columns("spots"))
            }
        assert "spots_fts_ai" in after and "health" in cols
    finally:
        await engine.dispose()
