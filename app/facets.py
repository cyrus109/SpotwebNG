"""Facet counts for the browse sidebar and the advanced-filter drawer.

Section counts feed the left rail; option counts feed the drawer so every
choice shows how many spots it would leave — no dead ends.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .filters import (
    _COLUMNS,
    _DIMENSIONS,
    FILTER_SECTIONS,
    base_tree,
    category_data,
    cycle_tree,
    parse_tree,
    tree_conditions,
    visible_sections,
)
from .models import Spot


async def _count(session: AsyncSession, conds: list) -> int:
    stmt = select(func.count()).select_from(Spot)
    for c in conds:
        stmt = stmt.where(c)
    return (await session.execute(stmt)).scalar_one()


async def section_counts(session: AsyncSession) -> dict[str, int]:
    """Spot count per sidebar section (All, Movies, Series, ...)."""
    counts: dict[str, int] = {}
    for section in FILTER_SECTIONS:
        key = section["key"] or "all"
        counts[key] = await _count(session, tree_conditions(section["tree"]))
    return counts


async def option_counts(session: AsyncSession, tree: str) -> list[dict]:
    """Per-dimension options for the drawer, each with a count and state.

    A code's count is measured with the *other* dimensions still applied, so
    the number shown is what you would actually get by clicking it.
    """
    included, excluded = parse_tree(tree)
    if not included:
        return []
    cat, ztype = included[0]["cat"], included[0]["z"]
    letters = _DIMENSIONS.get((cat, ztype)) or _DIMENSIONS.get((cat, None)) or []

    data = category_data()
    names = data["codes"].get(str(cat), {})
    labels = data["labels"].get(str(cat), {})
    inc_codes = {c for g in included for codes in g["subs"].values() for c in codes}
    exc_codes = {c for g in excluded for codes in g["subs"].values() for c in codes}
    base = base_tree(tree)

    dims = []
    for letter in letters:
        # Drop this dimension from the tree so its options aren't self-limiting.
        others = ",".join(
            p for p in tree.split(",") if not p.lstrip("~!").startswith(f"{base}_{letter}")
        )
        base_conds = tree_conditions(others)
        column = _COLUMNS[letter]

        # One pass per dimension instead of one COUNT per code: the old loop
        # issued ~133 unindexable scans for Movies (measured at 1.2s a page).
        stmt = select(column, func.count()).group_by(column)
        for cond in base_conds:
            stmt = stmt.where(cond)
        tally: dict[str, int] = {}
        for packed, count in (await session.execute(stmt)).all():
            for code in (packed or "").split("|"):
                if code:
                    tally[code] = tally.get(code, 0) + count

        options = []
        for number, name in sorted(names.get(letter, {}).items(), key=lambda kv: int(kv[0])):
            code = f"{letter}{number}"
            state = "inc" if code in inc_codes else "exc" if code in exc_codes else ""
            total = tally.get(code, 0)
            if total or state:
                options.append(
                    {
                        "code": code,
                        "name": name,
                        "count": total,
                        "state": state,
                        "tree": cycle_tree(tree, code),
                    }
                )
        if options:
            dims.append(
                {
                    "letter": letter,
                    "label": labels.get(letter, letter.upper()),
                    "options": options,
                    "chosen": sum(1 for o in options if o["state"]),
                }
            )
    return dims


async def section_dimensions(session: AsyncSession, hide_adult: bool = False) -> list[dict]:
    """Every category's dimensions, for refining from the "All" view.

    "All" spans four head categories whose sub-dimension letters mean different
    things (`c` is Language for video but Bitrate for audio), so there is no
    single coherent list — offer them grouped by category instead, and let each
    option carry the tree that both applies the filter and enters that section.
    """
    groups = []
    for section in visible_sections(hide_adult):
        if not section["tree"]:
            continue  # "All" is the thing we're refining *from*
        dims = await option_counts(session, section["tree"])
        if dims:
            groups.append(
                {
                    "key": section["key"],
                    "label": section["label"],
                    "tree": section["tree"],
                    "dimensions": dims,
                    "option_total": sum(len(d["options"]) for d in dims),
                }
            )
    return groups
