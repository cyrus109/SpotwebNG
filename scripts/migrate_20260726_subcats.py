"""Additive migration: sub-category columns for deep filtering.

Non-destructive — existing spots (and their fetched images/NZBs) are kept.
The new columns start empty; the retriever backfills them on the next scan
(see the backfill branch in retriever.retrieve_spots), which is triggered by
resetting the usenet_state bookmark so the recent range is re-read.
"""

import sqlite3
from pathlib import Path

db_path = Path(__file__).resolve().parent.parent / "spotwebnext.db"
conn = sqlite3.connect(db_path)

columns = {
    "subcat_a": "VARCHAR(64) DEFAULT ''",
    "subcat_b": "VARCHAR(64) DEFAULT ''",
    "subcat_c": "VARCHAR(64) DEFAULT ''",
    "subcat_d": "VARCHAR(128) DEFAULT ''",
    "subcat_z": "VARCHAR(32) DEFAULT ''",
    "spot_category": "INTEGER DEFAULT -1",
}
existing = {row[1] for row in conn.execute("PRAGMA table_info(spots)")}
for name, ddl in columns.items():
    if name not in existing:
        conn.execute(f"ALTER TABLE spots ADD COLUMN {name} {ddl}")
        print(f"added {name}")

conn.execute(
    "UPDATE usenet_state SET cur_article = 0, cur_message_id = '' WHERE infotype = 'spots'"
)
conn.commit()
print("bookmark reset — next scan backfills subcats for existing rows")
