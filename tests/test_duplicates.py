from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import duplicates, spotters
from app.models import Base, Spot

WHEN = datetime(2026, 7, 1, tzinfo=UTC)


@pytest.mark.parametrize(
    "better, worse",
    [
        ("Dune 2024 2160p BluRay x265", "Dune 2024 1080p BluRay x264"),
        ("Dune 2024 1080p BluRay", "Dune 2024 1080p WEB-DL"),
        ("Dune 2024 1080p WEB-DL", "Dune 2024 720p WEB-DL"),
        ("Dune 2024 1080p REMUX", "Dune 2024 1080p BluRay"),
        ("Dune 2024 720p", "Dune 2024 DVDRip"),
    ],
)
def test_quality_ranking_prefers_resolution_then_source(better, worse):
    assert duplicates.quality_score(better) > duplicates.quality_score(worse)


def test_best_breaks_ties_by_size():
    small = Spot(title="Dune 2024 1080p BluRay", size=10)
    big = Spot(title="Dune 2024 1080p BluRay", size=20)
    assert duplicates.best([small, big]) is big and duplicates.best([]) is None


@pytest.fixture
async def factory():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def test_counts_and_siblings_hide_adult_and_blocked_releases(factory):
    async with factory() as s:
        for i, (title, kw) in enumerate(
            [
                ("Dune 2024 2160p", {}),
                ("Dune 2024 1080p", {}),
                ("Dune 2024 720p", {"spotter_id": "SPAMID"}),  # blocked poster
                ("Dune 2024 XXX cut", {"adult": 1}),  # adult
                ("Unique Thing 2020", {}),
            ]
        ):
            s.add(
                Spot(
                    message_id=f"<d{i}@x>",
                    title=title,
                    title_key="dune 2024" if "Dune" in title else "unique thing 2020",
                    posted_at=WHEN,
                    adult=kw.pop("adult", 0),
                    **kw,
                )
            )
        await s.commit()
        await spotters.set_status(s, "SPAMID", "blocked")
        from sqlalchemy import select

        rows = list((await s.execute(select(Spot).order_by(Spot.id))).scalars().all())
        counts = await duplicates.release_counts(s, rows, hide_adult=True)
        assert counts == {
            "dune 2024": 2
        }  # the group of one has no badge; blocked/adult are not counted
        assert (await duplicates.release_counts(s, rows, hide_adult=False)) == {"dune 2024": 3}
        others = await duplicates.siblings(s, rows[0], hide_adult=True)
        assert [o.title for o in others] == ["Dune 2024 1080p"]
        assert await duplicates.siblings(s, rows[4], hide_adult=True) == []
        assert await duplicates.siblings(s, Spot(id=99, title="x", title_key=""), True) == []
