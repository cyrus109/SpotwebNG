"""One-time migration: rebuild the spots table for the full-XML feature.

The spots table gains columns (enriched/website/nzb_segments) and every row
needs a category re-map (new 4000/6000/7020 trees) that requires re-parsing
the original From headers, which we did not store. Spots are a re-fetchable
cache of Usenet headers (a fresh retrieval takes ~30s), so the table and the
retrieval bookmark are rebuilt from scratch. api_users and settings are kept.

A file backup is made by the caller before running (see session log).
"""

import sqlite3
from pathlib import Path

db_path = Path(__file__).resolve().parent.parent / "spotwebnext.db"
conn = sqlite3.connect(db_path)
conn.execute("DROP TABLE IF EXISTS spots")
conn.execute("DROP TABLE IF EXISTS usenet_state")
conn.commit()
tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
print(f"remaining tables: {tables}")
