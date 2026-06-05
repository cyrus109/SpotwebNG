"""Newznab category map + helpers.

Newznab uses numeric category IDs (2000=Movies, 5000=TV, 3000=Audio, etc.).
This is the subset Radarr/Sonarr/Prowlarr query and that Spotnet content maps to.
"""
from __future__ import annotations

# (id, name, parent_id) — parent_id 0 means top-level.
CATEGORIES: list[tuple[int, str, int]] = [
    (2000, "Movies", 0),
    (2030, "Movies/SD", 2000),
    (2040, "Movies/HD", 2000),
    (2045, "Movies/UHD", 2000),
    (2050, "Movies/BluRay", 2000),
    (2060, "Movies/3D", 2000),
    (5000, "TV", 0),
    (5030, "TV/SD", 5000),
    (5040, "TV/HD", 5000),
    (5045, "TV/UHD", 5000),
    (5070, "TV/Anime", 5000),
    (3000, "Audio", 0),
    (3010, "Audio/MP3", 3000),
    (3040, "Audio/Lossless", 3000),
    (7000, "Books", 0),
    (8000, "Other", 0),
]

CATEGORY_NAMES: dict[int, str] = {cid: name for cid, name, _ in CATEGORIES}


def expand_categories(requested: list[int]) -> set[int]:
    """Expand top-level cats (e.g. 2000) to include their children (2030, 2040...)."""
    out: set[int] = set()
    for cid in requested:
        out.add(cid)
        for child, _name, parent in CATEGORIES:
            if parent == cid:
                out.add(child)
    return out
