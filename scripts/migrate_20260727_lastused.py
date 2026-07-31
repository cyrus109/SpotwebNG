"""Additive migration: record when each API key was last used."""

import sqlite3
from pathlib import Path

conn = sqlite3.connect(Path(__file__).resolve().parent.parent / "spotwebnext.db")
cols = {r[1] for r in conn.execute("PRAGMA table_info(api_users)")}
if "last_used" not in cols:
    conn.execute("ALTER TABLE api_users ADD COLUMN last_used DATETIME")
    conn.commit()
    print("added api_users.last_used")
else:
    print("already present")
