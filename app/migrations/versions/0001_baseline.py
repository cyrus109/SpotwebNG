"""baseline

Revision ID: 0001
Revises:
Create Date: 2026-10-19 09:00:28.289160
"""

import sqlalchemy as sa

from app.migrations.helpers import _create_indexes, _create_or_extend

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:

    _create_or_extend(
        "api_users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("api_key", sa.String(length=128), nullable=False),
        sa.Column("last_used", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    _create_indexes(
        "api_users",
        [
            ("ix_api_users_api_key", ["api_key"], True),
        ],
    )

    _create_or_extend(
        "comments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("message_id", sa.String(length=255), nullable=False),
        sa.Column("spot_message_id", sa.String(length=255), nullable=False),
        sa.Column("poster", sa.String(length=80), nullable=False),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("body_state", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    _create_indexes(
        "comments",
        [
            ("ix_comments_message_id", ["message_id"], True),
            ("ix_comments_posted_at", ["posted_at"], False),
            ("ix_comments_spot_message_id", ["spot_message_id"], False),
        ],
    )

    _create_or_extend(
        "downloads",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("message_id", sa.String(length=255), nullable=False),
        sa.Column("first_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    _create_indexes(
        "downloads",
        [
            ("ix_downloads_message_id", ["message_id"], True),
        ],
    )

    _create_or_extend(
        "sessions",
        sa.Column("token", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("token"),
    )
    _create_indexes(
        "sessions",
        [
            ("ix_sessions_user_id", ["user_id"], False),
        ],
    )

    _create_or_extend(
        "settings",
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )
    _create_or_extend(
        "spots",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("message_id", sa.String(length=255), nullable=False),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("poster", sa.String(length=255), nullable=False),
        sa.Column("category", sa.Integer(), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("subcat_a", sa.String(length=64), nullable=False),
        sa.Column("subcat_b", sa.String(length=64), nullable=False),
        sa.Column("subcat_c", sa.String(length=64), nullable=False),
        sa.Column("subcat_d", sa.String(length=128), nullable=False),
        sa.Column("subcat_z", sa.String(length=32), nullable=False),
        sa.Column("spot_category", sa.Integer(), nullable=False),
        sa.Column("imdb_id", sa.String(length=16), nullable=False),
        sa.Column("tvmaze_id", sa.String(length=16), nullable=False),
        sa.Column("poster_url", sa.String(length=1024), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("nzb_ref", sa.String(length=255), nullable=False),
        sa.Column("enriched", sa.Integer(), nullable=False),
        sa.Column("website", sa.String(length=1024), nullable=False),
        sa.Column("nzb_segments", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    _create_indexes(
        "spots",
        [
            ("ix_spots_category", ["category"], False),
            ("ix_spots_enriched", ["enriched"], False),
            ("ix_spots_imdb_id", ["imdb_id"], False),
            ("ix_spots_message_id", ["message_id"], True),
            ("ix_spots_posted_at", ["posted_at"], False),
            ("ix_spots_spot_category", ["spot_category"], False),
            ("ix_spots_title", ["title"], False),
        ],
    )

    _create_or_extend(
        "usenet_state",
        sa.Column("infotype", sa.String(length=32), nullable=False),
        sa.Column("cur_article", sa.BigInteger(), nullable=False),
        sa.Column("cur_message_id", sa.String(length=255), nullable=False),
        sa.Column(
            "last_retrieved",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("infotype"),
    )
    _create_or_extend(
        "users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=64), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column("last_login", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    _create_indexes(
        "users",
        [
            ("ix_users_role", ["role"], False),
            ("ix_users_username", ["username"], True),
        ],
    )


def downgrade() -> None:
    raise NotImplementedError("forward-only: restore the pre-migration backup to roll back")
