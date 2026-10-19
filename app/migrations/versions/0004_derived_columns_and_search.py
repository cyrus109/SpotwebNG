"""derived columns (adult flag, normalised title) and full-text search plumbing

Revision ID: 0004
Revises: 0003
"""

import sqlalchemy as sa
from alembic import op

from app.migrations.helpers import _create_indexes, add_column_if_missing

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    add_column_if_missing(
        "spots", sa.Column("adult", sa.Integer(), nullable=False, server_default=sa.text("-1"))
    )
    add_column_if_missing(
        "spots",
        sa.Column("title_key", sa.String(255), nullable=False, server_default=sa.text("''")),
    )
    _create_indexes(
        "spots",
        [("ix_spots_adult", ["adult"], False), ("ix_spots_title_key", ["title_key"], False)],
    )
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        # External-content FTS5 table over spots. Only the INSERT trigger exists here:
        # UPDATE/DELETE triggers on an index that has not been built yet would corrupt it,
        # so the `search-index` job builds the index and then adds them (see derived.py).
        try:
            bind.exec_driver_sql(
                "CREATE VIRTUAL TABLE IF NOT EXISTS spots_fts USING fts5("
                "title, description, content='spots', content_rowid='id', "
                "tokenize='unicode61 remove_diacritics 2')"
            )
            bind.exec_driver_sql(
                "CREATE TRIGGER IF NOT EXISTS spots_fts_ai AFTER INSERT ON spots BEGIN "
                "INSERT INTO spots_fts(rowid, title, description) "
                "VALUES (new.id, new.title, new.description); END"
            )
        except Exception:  # SQLite built without FTS5: search keeps using LIKE
            pass
    elif bind.dialect.name == "postgresql":
        bind.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        bind.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_spots_title_trgm ON spots USING gin (title gin_trgm_ops)"
        )


def downgrade() -> None:
    raise NotImplementedError("forward-only: restore the pre-migration backup to roll back")
