"""Tests for typed application settings and the retention pruner."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import Base, Spot
from app.retention import prune_old_spots
from app.settings_store import DEFAULTS, get_setting, load_settings, save_settings

NOW = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        yield s
    await engine.dispose()


async def test_defaults_apply_when_nothing_is_stored(session):
    values = await load_settings(session)
    assert values["per_page"] == DEFAULTS["per_page"]
    assert values["retention_days"] == 0  # 0 = keep forever
    assert values["retention_type"] == "fullonly"
    assert values["hide_adult"] is False


async def test_settings_round_trip_with_types(session):
    await save_settings(session, {"per_page": "60", "hide_adult": "on", "retention_days": "90"})
    values = await load_settings(session)
    assert values["per_page"] == 60  # int, not "60"
    assert values["hide_adult"] is True  # checkbox -> bool
    assert values["retention_days"] == 90


async def test_unknown_keys_are_ignored(session):
    await save_settings(session, {"per_page": "40", "evil": "1"})
    values = await load_settings(session)
    assert values["per_page"] == 40
    assert "evil" not in values


async def test_invalid_numbers_fall_back_to_the_default(session):
    await save_settings(session, {"per_page": "not-a-number"})
    assert (await load_settings(session))["per_page"] == DEFAULTS["per_page"]


async def test_per_page_is_clamped(session):
    await save_settings(session, {"per_page": "9999"})
    assert (await load_settings(session))["per_page"] <= 500


async def test_get_setting_reads_one_value(session):
    await save_settings(session, {"default_sort": "old"})
    assert await get_setting(session, "default_sort") == "old"


# ---------- retention ----------


def _spot(n: int, days_old: int, enriched: int = 1) -> Spot:
    return Spot(
        message_id=f"<r{n}@x>",
        title=f"Old {n}",
        posted_at=NOW - timedelta(days=days_old),
        category=2040,
        enriched=enriched,
        description="x" * 100,
        poster_url=f"/image/{n}",
        nzb_segments='["a@b"]',
    )


async def test_retention_disabled_keeps_everything(session):
    session.add_all([_spot(1, 400), _spot(2, 5)])
    await session.commit()
    result = await prune_old_spots(session, now=NOW)
    assert result["deleted"] == 0 and result["stripped"] == 0
    assert (await session.execute(select(func.count()).select_from(Spot))).scalar_one() == 2


async def test_fullonly_strips_heavy_fields_but_keeps_the_spot(session):
    session.add_all([_spot(1, 400), _spot(2, 5)])
    await save_settings(session, {"retention_days": "90", "retention_type": "fullonly"})
    await session.commit()

    result = await prune_old_spots(session, now=NOW)
    assert result["stripped"] == 1 and result["deleted"] == 0
    assert (await session.execute(select(func.count()).select_from(Spot))).scalar_one() == 2

    old = (await session.execute(select(Spot).where(Spot.message_id == "<r1@x>"))).scalar_one()
    assert old.poster_url == "" and old.nzb_segments == ""
    assert old.enriched == 3  # pruned marker: never re-enrich
    assert old.title == "Old 1"  # still searchable

    fresh = (await session.execute(select(Spot).where(Spot.message_id == "<r2@x>"))).scalar_one()
    assert fresh.poster_url == "/image/2"


async def test_everything_deletes_old_spots(session):
    session.add_all([_spot(1, 400), _spot(2, 5)])
    await save_settings(session, {"retention_days": "90", "retention_type": "everything"})
    await session.commit()

    result = await prune_old_spots(session, now=NOW)
    assert result["deleted"] == 1
    remaining = (await session.execute(select(Spot))).scalars().all()
    assert [s.message_id for s in remaining] == ["<r2@x>"]


async def test_pruning_is_idempotent(session):
    session.add_all([_spot(1, 400)])
    await save_settings(session, {"retention_days": "90", "retention_type": "fullonly"})
    await session.commit()
    first = await prune_old_spots(session, now=NOW)
    second = await prune_old_spots(session, now=NOW)
    assert first["stripped"] == 1 and second["stripped"] == 0


# ---------- hide_adult must be consistent everywhere ----------


async def test_hide_adult_removes_the_erotica_section(session):
    from app.filters import visible_sections

    assert any(s["key"] == "erotica" for s in visible_sections(hide_adult=False))
    assert not any(s["key"] == "erotica" for s in visible_sections(hide_adult=True))


def test_tree_targets_adult_detects_the_erotica_branch():
    from app.filters import tree_targets_adult

    assert tree_targets_adult("cat0_z3") is True
    assert tree_targets_adult("cat0_z3,cat0_z3_d75") is True
    assert tree_targets_adult("cat0_z0") is False
    assert tree_targets_adult("") is False
    assert tree_targets_adult("cat0,~cat0_z3") is False  # excluding it is not targeting it


# ---------- theme ----------


async def test_theme_defaults_and_choices(session):
    values = await load_settings(session)
    assert values["theme"] == "liquid-glass"

    await save_settings(session, {"theme": "midnight"})
    assert (await load_settings(session))["theme"] == "midnight"

    # an unknown theme must not reach the html attribute
    await save_settings(session, {"theme": "<script>"})
    assert (await load_settings(session))["theme"] == "liquid-glass"
