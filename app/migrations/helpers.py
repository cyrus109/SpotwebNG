"""Idempotent building blocks for migrations: every revision must be safe to run
on a database that already has (part of) its changes."""

import sqlalchemy as sa
from alembic import op


def _create_or_extend(name: str, *items, **kw) -> None:
    """Create the table, or on an install that predates migrations add whatever
    columns it is missing — a baseline must be safe on every existing database."""
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if name not in insp.get_table_names():
        op.create_table(name, *items, **kw)
        return
    have = {c["name"] for c in insp.get_columns(name)}
    for item in items:
        if isinstance(item, sa.Column) and item.name not in have:
            op.add_column(name, item)


def _create_indexes(table: str, indexes: list[tuple[str, list[str], bool]]) -> None:
    insp = sa.inspect(op.get_bind())
    have = {i["name"] for i in insp.get_indexes(table)}
    for name, cols, unique in indexes:
        if name not in have:
            op.create_index(name, table, cols, unique=unique)


def add_column_if_missing(table: str, column: sa.Column) -> None:
    insp = sa.inspect(op.get_bind())
    if column.name not in {c["name"] for c in insp.get_columns(table)}:
        # Plain ALTER TABLE ADD COLUMN, never batch mode: on SQLite batch rebuilds the whole
        # table, which silently drops its triggers (the FTS5 search index depends on them).
        op.add_column(table, column)
