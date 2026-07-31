"""Tests for the sidebar section counts, facet option counts, and 3-state toggling."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.facets import option_counts, section_counts
from app.filters import cycle_tree, parse_tree
from app.models import Base, Spot

NOW = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)


def _spot(n: int, **kw) -> Spot:
    d = dict(
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
    d.update(kw)
    return Spot(**d)


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        s.add_all(
            [
                _spot(1),
                _spot(2, subcat_a="a15|"),
                _spot(3, subcat_z="z1|"),  # series
                _spot(4, subcat_z="z2|"),  # book
                _spot(5, subcat_z="z3|"),  # erotica
                _spot(6, spot_category=1, subcat_a="a8|"),  # music
                _spot(7, spot_category=2, subcat_a="a0|", subcat_z=""),  # game
                _spot(8, spot_category=3, subcat_a="a7|", subcat_z=""),  # app
            ]
        )
        await s.commit()
        yield s
    await engine.dispose()


async def test_section_counts_cover_every_sidebar_entry(session):
    counts = await section_counts(session)
    assert counts["all"] == 8
    assert counts["movies"] == 2  # z0
    assert counts["series"] == 1
    assert counts["books"] == 1
    assert counts["erotica"] == 1
    assert counts["music"] == 1
    assert counts["games"] == 1
    assert counts["apps"] == 1


async def test_option_counts_are_scoped_to_the_section(session):
    dims = await option_counts(session, "cat0_z0")
    fmt = next(d for d in dims if d["letter"] == "a")
    codes = {o["code"]: o["count"] for o in fmt["options"]}
    assert codes["a9"] == 1  # only the movie, not the series/book/erotica rows
    assert codes["a15"] == 1
    assert "a0" not in codes  # zero-count options are dropped


async def test_option_counts_narrow_with_the_current_selection(session):
    """Counts reflect what's still reachable given the other active filters."""
    dims = await option_counts(session, "cat0_z0,cat0_z0_a9")
    lang = next(d for d in dims if d["letter"] == "c")
    codes = {o["code"]: o["count"] for o in lang["options"]}
    assert codes["c2"] == 1  # the a15 spot is excluded by the format filter


async def test_option_counts_marks_active_and_excluded(session):
    dims = await option_counts(session, "cat0_z0,cat0_z0_a9,~cat0_z0_a15")
    fmt = next(d for d in dims if d["letter"] == "a")
    by_code = {o["code"]: o for o in fmt["options"]}
    assert by_code["a9"]["state"] == "inc"
    assert by_code["a15"]["state"] == "exc"


async def test_option_counts_uses_the_right_labels_per_category(session):
    music = await option_counts(session, "cat1")
    labels = {d["letter"]: d["label"] for d in music}
    assert labels["c"] == "Bitrate"  # not "Language" — cat1 differs
    movies = await option_counts(session, "cat0_z0")
    assert next(d for d in movies if d["letter"] == "c")["label"] == "Language"


# ---------- 3-state cycling: off -> include -> exclude -> off ----------


def test_cycle_adds_include_then_exclude_then_clears():
    t1 = cycle_tree("cat0_z0", "a9")
    assert parse_tree(t1)[0][0]["subs"] == {"a": ["a9"]}

    t2 = cycle_tree(t1, "a9")
    inc, exc = parse_tree(t2)
    assert inc[0]["subs"] == {}
    assert exc[0]["subs"] == {"a": ["a9"]}

    t3 = cycle_tree(t2, "a9")
    inc, exc = parse_tree(t3)
    assert inc[0]["subs"] == {}
    assert exc == []


def test_cycle_keeps_other_selections():
    tree = cycle_tree(cycle_tree("cat0_z0", "a9"), "c2")
    inc, _ = parse_tree(tree)
    assert inc[0]["subs"] == {"a": ["a9"], "c": ["c2"]}
    after = cycle_tree(tree, "a9")  # a9 -> exclude, c2 untouched
    inc, exc = parse_tree(after)
    assert inc[0]["subs"] == {"c": ["c2"]}
    assert exc[0]["subs"] == {"a": ["a9"]}


def test_cycle_on_empty_tree_is_a_noop():
    assert cycle_tree("", "a9") == ""


# ---------- browsing "All": filters grouped by category ----------


async def test_section_dimensions_lists_every_browsable_section(session):
    """The All view has no single dimension set, so offer them per category."""
    from app.facets import section_dimensions

    groups = await section_dimensions(session)
    keys = [g["key"] for g in groups]
    assert "movies" in keys and "music" in keys and "games" in keys
    assert "" not in keys  # "All" itself is not a refinable group

    movies = next(g for g in groups if g["key"] == "movies")
    assert movies["label"] == "Movies"
    assert movies["tree"] == "cat0_z0"
    assert [d["label"] for d in movies["dimensions"]][:2] == ["Format", "Source"]


async def test_section_dimensions_option_trees_switch_into_that_section(session):
    """Picking Movies > x264 from All must both filter and enter Movies."""
    from app.facets import section_dimensions

    groups = await section_dimensions(session)
    movies = next(g for g in groups if g["key"] == "movies")
    fmt = next(d for d in movies["dimensions"] if d["letter"] == "a")
    a9 = next((o for o in fmt["options"] if o["code"] == "a9"), None)
    assert a9 is not None
    assert a9["tree"].startswith("cat0_z0")
    assert "cat0_z0_a9" in a9["tree"]


async def test_section_dimensions_hides_adult_when_asked(session):
    from app.facets import section_dimensions

    groups = await section_dimensions(session, hide_adult=True)
    assert "erotica" not in [g["key"] for g in groups]
