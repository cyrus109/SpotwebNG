"""Demo seed data.

So the UI and Newznab API are immediately alive without a live NNTP connection.
In production the engine (legacy retrieve.php scaffold in Phase 1, native Python
in Phase 3) populates the `spots` table instead. Safe to delete.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import get_settings
from .models import ApiUser, Spot

_NOW = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)

_DEMO_SPOTS = [
    # title, category, size_gb, imdb, poster_url, description
    ("Dune Part Two 2024 2160p UHD BluRay x265", 2045, 24.6, "15239678",
     "https://image.tmdb.org/t/p/w500/1pdfLvkbY9ohJlCjQH2CZjjYVvJ.jpg",
     "Paul Atreides unites with the Fremen to wage war against House Harkonnen."),
    ("Oppenheimer 2023 1080p BluRay x264", 2040, 12.1, "15398776",
     "https://image.tmdb.org/t/p/w500/8Gxv8gSFCU0XGDykEGv7zR1n2ua.jpg",
     "The story of J. Robert Oppenheimer and the atomic bomb."),
    ("The Bear S03 1080p WEB-DL DDP5.1 x264", 5040, 18.3, "",
     "https://image.tmdb.org/t/p/w500/zPyTjjwl7BCBNvLO9ehNRMVMqyR.jpg",
     "A young chef returns to Chicago to run his family sandwich shop."),
    ("Shogun S01 2160p WEB-DL DV HDR x265", 5045, 41.0, "",
     "https://image.tmdb.org/t/p/w500/7O4iVfOMQmdCSxhOg1WnzgZBT1o.jpg",
     "A tale of feudal Japan and a stranded English navigator."),
    ("Poor Things 2023 1080p BluRay DTS x264", 2040, 13.7, "14230458",
     "https://image.tmdb.org/t/p/w500/kCGlIMHnOm8JPXq3rXM6c5wMxcT.jpg",
     "The incredible tale of Bella Baxter, brought back to life by a scientist."),
    ("Fallout S01 2160p AMZN WEB-DL DDP5.1 x265", 5045, 38.5, "",
     "https://image.tmdb.org/t/p/w500/AnsSKR9LuK0T9bAGV5Kb8Ac9HOd.jpg",
     "200 years after the apocalypse, survivors leave their fallout shelters."),
    ("The Zone of Interest 2023 1080p BluRay x264", 2040, 9.8, "7160372",
     "https://image.tmdb.org/t/p/w500/eYbcZUu8oVfJZARd2D9SCfO9hMy.jpg",
     "The commandant of Auschwitz builds an idyllic life beside the camp."),
    ("True Detective S04 Night Country 1080p WEB x264", 5040, 16.2, "",
     "https://image.tmdb.org/t/p/w500/jVNg2qjBQA9MFAhCMtsT3Az4tdr.jpg",
     "Two detectives investigate disappearances in the Alaskan night."),
]


async def seed_if_empty(session: AsyncSession) -> None:
    settings = get_settings()

    # Ensure a default API user exists (the key the client authenticates with).
    existing_key = (
        await session.execute(select(ApiUser).where(ApiUser.api_key == settings.default_api_key))
    ).scalar_one_or_none()
    if existing_key is None:
        session.add(ApiUser(name="default", api_key=settings.default_api_key))

    count = (await session.execute(select(func.count()).select_from(Spot))).scalar_one()
    if settings.seed_demo_data and count == 0:
        for i, (title, cat, gb, imdb, poster, desc) in enumerate(_DEMO_SPOTS):
            session.add(
                Spot(
                    message_id=f"<demo-{i}@spotwebnext>",
                    title=title,
                    poster="SpotwebNG-Demo",
                    category=cat,
                    size=int(gb * 1024**3),
                    posted_at=_NOW - timedelta(hours=i * 7),
                    imdb_id=imdb,
                    poster_url=poster,
                    description=desc,
                    nzb_ref=f"<demo-nzb-{i}@spotwebnext>",
                )
            )
    await session.commit()
