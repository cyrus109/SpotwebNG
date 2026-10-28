"""one index for "newest unenriched spots" instead of a sort over a million rows

Revision ID: 0008
Revises: 0007

The enrichment loop runs WHERE enriched = 0 ORDER BY posted_at DESC LIMIT 50. SQLite could use
ix_spots_enriched only for the filter, then had to sort every unenriched row (USE TEMP B-TREE
FOR ORDER BY): on a 1.4 million row table on a spinning disk, that was most of the CPU and I/O
load. (enriched, posted_at) serves the whole query from the index. It also makes the old
single-column index redundant, so it is dropped (one index less to maintain on every update).
"""

import sqlalchemy as sa
from alembic import op

from app.migrations.helpers import _create_indexes

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    _create_indexes("spots", [("ix_spots_enriched_posted_at", ["enriched", "posted_at"], False)])
    have = {i["name"] for i in sa.inspect(op.get_bind()).get_indexes("spots")}
    if "ix_spots_enriched" in have:
        op.drop_index("ix_spots_enriched", table_name="spots")


def downgrade() -> None:
    raise NotImplementedError("forward-only: restore the pre-migration backup to roll back")
