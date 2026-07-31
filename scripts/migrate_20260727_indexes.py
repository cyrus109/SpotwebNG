"""Add the indexes the ORM declares but ALTER TABLE never created."""

import sqlite3
from pathlib import Path

conn = sqlite3.connect(Path(__file__).resolve().parent.parent / "spotwebnext.db")
for name, ddl in {
    "ix_spots_spot_category": "CREATE INDEX IF NOT EXISTS ix_spots_spot_category ON spots(spot_category)",
    "ix_spots_cat_z": "CREATE INDEX IF NOT EXISTS ix_spots_cat_z ON spots(spot_category, subcat_z)",
    "ix_sessions_expires": "CREATE INDEX IF NOT EXISTS ix_sessions_expires ON sessions(expires_at)",
}.items():
    conn.execute(ddl)
    print("ensured", name)
conn.commit()
conn.execute("ANALYZE")
conn.commit()
