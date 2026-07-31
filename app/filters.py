"""Spotnet category filtering — port of Spotweb's tree-string filter syntax.

A `tree` is a comma-separated selection over the Spotnet category hierarchy:

    cat0            whole head category (Image)
    cat0_z0         one type (Movie)
    cat0_z0_a9      one leaf (x264)
    ~cat0_z3        strong NOT — excluded via SQL NOT

Combination rules (Spotweb's Services_Search_QueryParser::categoryListToSql):
values inside one sub-dimension are OR'ed, different dimensions are AND'ed,
and separate (category, type) groups are OR'ed together.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from typing import Any

from sqlalchemy import and_, not_, or_

from .config import DATA_DIR
from .models import Spot

# subcat_a / subcat_z hold a single value; b/c/d may hold several.
SINGLE_VALUE_LETTERS = ("a", "z")

_TOKEN = re.compile(r"^(?P<neg>[~!])?cat(?P<cat>\d)(?:_z(?P<z>\d+))?(?:_(?P<sub>[a-z]\d*))?$")

_COLUMNS = {
    "a": Spot.subcat_a,
    "b": Spot.subcat_b,
    "c": Spot.subcat_c,
    "d": Spot.subcat_d,
    "z": Spot.subcat_z,
}


@lru_cache(maxsize=1)
def category_data() -> dict:
    """Spotnet category names/labels, extracted from Spotweb's SpotCategories."""
    with open(DATA_DIR / "spot_categories.json", encoding="utf-8") as fh:
        return json.load(fh)


def parse_tree(tree: str) -> tuple[list[dict], list[dict]]:
    """Parse a tree string into (included groups, strong-NOT groups).

    Each group is {"cat": int, "z": "z0"|None, "subs": {letter: [codes]}}.
    Tokens sharing a (cat, z) pair are merged so their dimensions combine.
    """
    included: list[dict] = []
    excluded: list[dict] = []

    for raw in (tree or "").split(","):
        token = raw.strip()
        if not token:
            continue
        match = _TOKEN.match(token)
        if not match:
            continue
        target = excluded if match.group("neg") else included
        cat = int(match.group("cat"))
        z = f"z{match.group('z')}" if match.group("z") is not None else None

        group = next((g for g in target if g["cat"] == cat and g["z"] == z), None)
        if group is None:
            group = {"cat": cat, "z": z, "subs": {}}
            target.append(group)

        sub = match.group("sub")
        if sub:
            letter, code = sub[0], sub[1:]
            if letter in _COLUMNS and code:  # `cat0_z0_a` (whole dimension) adds nothing
                group["subs"].setdefault(letter, []).append(f"{letter}{int(code)}")
    return included, excluded


def _match_code(letter: str, code: str) -> Any:
    """One code → SQL. Single-value columns compare exactly, multi-value use
    a boundary-safe LIKE so d5 never matches d41 or d55."""
    column = _COLUMNS[letter]
    if letter in SINGLE_VALUE_LETTERS:
        return column == f"{code}|"
    return column.like(f"%{code}|%")


def _group_condition(group: dict) -> Any:
    conds = [Spot.spot_category == group["cat"]]
    if group["z"]:
        conds.append(Spot.subcat_z == f"{group['z']}|")
    for letter, codes in group["subs"].items():
        conds.append(or_(*[_match_code(letter, code) for code in codes]))
    return and_(*conds)


def tree_conditions(tree: str) -> list:
    """SQLAlchemy conditions for a tree string (empty list = no filtering)."""
    included, excluded = parse_tree(tree)
    conds = []
    if included:
        conds.append(or_(*[_group_condition(g) for g in included]))
    for group in excluded:
        conds.append(not_(_group_condition(group)))
    return conds


# ---------- browse UI: sections + contextual sub-filters ----------

# Top-level sections, mirroring Spotweb's shipped filter tree.
FILTER_SECTIONS = [
    {"key": "", "label": "All", "tree": ""},
    {"key": "movies", "label": "Movies", "tree": "cat0_z0"},
    {"key": "series", "label": "Series", "tree": "cat0_z1"},
    {"key": "books", "label": "Books", "tree": "cat0_z2"},
    {"key": "music", "label": "Music", "tree": "cat1"},
    {"key": "games", "label": "Games", "tree": "cat2"},
    {"key": "apps", "label": "Apps", "tree": "cat3"},
    {"key": "erotica", "label": "Erotica", "tree": "cat0_z3"},
]

# Which sub-dimensions to offer per (cat, ztype); order = display order.
_DIMENSIONS = {
    (0, "z0"): ["a", "b", "c", "d"],  # Movies: Format, Source, Language, Genre
    (0, "z1"): ["a", "b", "c", "d"],  # Series
    (0, "z2"): ["a", "c"],  # Books: Format, Language
    (0, "z3"): ["a", "d"],  # Erotica: Format, Genre
    (1, None): ["a", "c", "d"],  # Music: Format, Bitrate, Genre
    (2, None): ["a", "b", "c"],  # Games: Platform, Format, Genre
    (3, None): ["a", "b"],  # Apps: Platform, Genre
}


def section_for(tree: str) -> str:
    """Which top-level section a tree belongs to (by its first group)."""
    included, _ = parse_tree(tree)
    if not included:
        return ""
    cat, z = included[0]["cat"], included[0]["z"]
    for section in FILTER_SECTIONS:
        if not section["tree"]:
            continue
        s_inc, _ = parse_tree(section["tree"])
        if s_inc[0]["cat"] == cat and s_inc[0]["z"] == z:
            return section["key"]
    return ""


def active_codes(tree: str) -> set[str]:
    """Every leaf code currently selected, e.g. {'a9', 'c2'}."""
    included, _ = parse_tree(tree)
    return {code for group in included for codes in group["subs"].values() for code in codes}


def base_tree(tree: str) -> str:
    """The tree with sub-dimension leaves stripped — just the section."""
    included, _ = parse_tree(tree)
    if not included:
        return ""
    cat, z = included[0]["cat"], included[0]["z"]
    return f"cat{cat}" + (f"_{z}" if z else "")


def toggle_tree(tree: str, code: str) -> str:
    """The tree you get by toggling one sub-filter code on or off."""
    base = base_tree(tree)
    if not base:
        return ""
    codes = active_codes(tree)
    codes.symmetric_difference_update({code})
    return ",".join([base] + [f"{base}_{c}" for c in sorted(codes)])


def subfilters_for(tree: str) -> list[dict]:
    """Sub-dimension options for the current section, with names and hrefs."""
    included, _ = parse_tree(tree)
    if not included:
        return []
    cat, z = included[0]["cat"], included[0]["z"]
    letters = _DIMENSIONS.get((cat, z)) or _DIMENSIONS.get((cat, None)) or []
    data = category_data()
    names = data["codes"].get(str(cat), {})
    labels = data["labels"].get(str(cat), {})
    selected = active_codes(tree)

    sections = []
    for letter in letters:
        options = []
        for number, name in sorted(names.get(letter, {}).items(), key=lambda kv: int(kv[0])):
            code = f"{letter}{number}"
            options.append(
                {
                    "code": code,
                    "name": name,
                    "active": code in selected,
                    "tree": toggle_tree(tree, code),
                }
            )
        if options:
            sections.append(
                {
                    "letter": letter,
                    "label": labels.get(letter, letter.upper()),
                    "options": options,
                }
            )
    return sections


def cycle_tree(tree: str, code: str) -> str:
    """Cycle one code through off → include → exclude → off.

    Mirrors Spotweb's tree checkbox behaviour, where unchecking a selected node
    turns it into a strong-not (`~`) before clearing it.
    """
    base = base_tree(tree)
    if not base:
        return tree
    included, excluded = parse_tree(tree)
    inc = {c for g in included for codes in g["subs"].values() for c in codes}
    exc = {c for g in excluded for codes in g["subs"].values() for c in codes}

    if code in inc:
        inc.discard(code)
        exc.add(code)
    elif code in exc:
        exc.discard(code)
    else:
        inc.add(code)

    parts = [base]
    parts += [f"{base}_{c}" for c in sorted(inc)]
    parts += [f"~{base}_{c}" for c in sorted(exc)]
    return ",".join(parts)


ADULT_TREE = "cat0_z3"


def visible_sections(hide_adult: bool) -> list[dict]:
    """Sections for the browse rail; Erotica disappears when hiding adult content."""
    if not hide_adult:
        return FILTER_SECTIONS
    return [s for s in FILTER_SECTIONS if s["tree"] != ADULT_TREE]


def tree_targets_adult(tree: str) -> bool:
    """True when the selection actively asks for the adult branch (not excludes it)."""
    included, _ = parse_tree(tree)
    return any(g["cat"] == 0 and g["z"] == "z3" for g in included)
