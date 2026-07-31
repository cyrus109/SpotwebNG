"""Tests for Spotweb's `tree` filter syntax and the value filters.

Grammar (verified against spotweb/spotweb Services_Search_QueryParser):
  cat0                 whole head category
  cat0_z0              one type (Movie/Series/Book/Erotica)
  cat0_z0_a            a whole sub-dimension
  cat0_z0_a9           one leaf
  ~cat0_z3             strong NOT (excluded with SQL NOT)
  comma-separated; OR within a dimension, AND across dimensions.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.filters import parse_tree
from app.models import Base, Spot
from app.spots import search_spots

NOW = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)


def _spot(n: int, **kw) -> Spot:
    defaults = dict(
        message_id=f"<s{n}@x>",
        title=f"Spot {n}",
        posted_at=NOW - timedelta(hours=n),
        category=2040,
        spot_category=0,
        subcat_a="a9|",
        subcat_b="b3|",
        subcat_c="c2|",
        subcat_d="d5|",
        subcat_z="z0|",
        size=1_000_000_000,
    )
    defaults.update(kw)
    return Spot(**defaults)


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        s.add_all(
            [
                _spot(1),  # x264 movie, Retail, NL subs, genre d5
                _spot(2, subcat_a="a15|", subcat_d="d7|"),  # UHD movie
                _spot(3, subcat_a="a0|", subcat_c="c3|"),  # DivX, EN subs
                _spot(4, subcat_z="z1|", subcat_b="b4|"),  # series
                _spot(5, subcat_z="z3|", subcat_d="d75|"),  # erotica
                _spot(6, subcat_d="d5|d41|"),  # multi-genre incl. d5
                _spot(7, spot_category=1, subcat_a="a8|", subcat_z="z0|"),  # FLAC sound
            ]
        )
        await s.commit()
        yield s
    await engine.dispose()


# ---------- tree grammar ----------


def test_parse_leaf_and_type():
    inc, notted = parse_tree("cat0_z0_a9")
    assert inc == [{"cat": 0, "z": "z0", "subs": {"a": ["a9"]}}]
    assert notted == []

    inc, _ = parse_tree("cat0_z1")
    assert inc == [{"cat": 0, "z": "z1", "subs": {}}]


def test_parse_head_category_only():
    inc, _ = parse_tree("cat1")
    assert inc == [{"cat": 1, "z": None, "subs": {}}]


def test_parse_groups_same_dimension_together():
    inc, _ = parse_tree("cat0_z0_a4,cat0_z0_a9,cat0_z0_a15")
    assert inc == [{"cat": 0, "z": "z0", "subs": {"a": ["a4", "a9", "a15"]}}]


def test_parse_strong_not():
    inc, notted = parse_tree("cat0,~cat0_z3")
    assert inc == [{"cat": 0, "z": None, "subs": {}}]
    assert notted == [{"cat": 0, "z": "z3", "subs": {}}]


def test_parse_ignores_junk():
    inc, notted = parse_tree("nonsense,,cat0_z0")
    assert inc == [{"cat": 0, "z": "z0", "subs": {}}]
    assert notted == []


# ---------- tree → results ----------


async def test_tree_single_leaf(session):
    rows, total = await search_spots(session, tree="cat0_z0_a9")
    assert {r.title for r in rows} == {"Spot 1", "Spot 6"}


async def test_tree_values_in_same_dimension_are_or(session):
    rows, _ = await search_spots(session, tree="cat0_z0_a0,cat0_z0_a15")
    assert {r.title for r in rows} == {"Spot 2", "Spot 3"}


async def test_tree_dimensions_are_and(session):
    rows, _ = await search_spots(session, tree="cat0_z0_a9,cat0_z0_c2")
    assert {r.title for r in rows} == {"Spot 1", "Spot 6"}
    rows, _ = await search_spots(session, tree="cat0_z0_a9,cat0_z0_c3")
    assert rows == []


async def test_tree_type_selects_all_of_that_type(session):
    rows, _ = await search_spots(session, tree="cat0_z1")
    assert {r.title for r in rows} == {"Spot 4"}


async def test_tree_head_category_ignores_type(session):
    rows, _ = await search_spots(session, tree="cat1")
    assert {r.title for r in rows} == {"Spot 7"}


async def test_tree_multi_value_column_uses_boundary_match(session):
    """d5 must not match d41/d55 — values are pipe-terminated."""
    rows, _ = await search_spots(session, tree="cat0_z0_d41")
    assert {r.title for r in rows} == {"Spot 6"}
    # spots 1, 3 and 6 all carry d5 (3 only overrides format/language)
    rows, _ = await search_spots(session, tree="cat0_z0_d5")
    assert {r.title for r in rows} == {"Spot 1", "Spot 3", "Spot 6"}


async def test_strong_not_excludes(session):
    rows, _ = await search_spots(session, tree="cat0,~cat0_z3")
    assert "Spot 5" not in {r.title for r in rows}
    assert "Spot 1" in {r.title for r in rows}


async def test_multiple_groups_are_or(session):
    rows, _ = await search_spots(session, tree="cat0_z1,cat1")
    assert {r.title for r in rows} == {"Spot 4", "Spot 7"}


# ---------- value filters ----------


async def test_size_range(session):
    rows, _ = await search_spots(session, min_size=2_000_000_000)
    assert rows == []
    _, total = await search_spots(session, max_size=2_000_000_000)
    assert total == 7


async def test_posted_since(session):
    _, total = await search_spots(session, since=NOW - timedelta(hours=3, minutes=30))
    assert total == 3


async def test_sorting(session):
    newest, _ = await search_spots(session, sort="new")
    assert newest[0].title == "Spot 1"
    oldest, _ = await search_spots(session, sort="old")
    assert oldest[0].title == "Spot 7"
    biggest, _ = await search_spots(session, sort="size")
    assert biggest[0].size == 1_000_000_000


async def test_tree_combines_with_text_search(session):
    rows, _ = await search_spots(session, query="Spot", tree="cat0_z0_a9", sort="new")
    assert {r.title for r in rows} == {"Spot 1", "Spot 6"}
