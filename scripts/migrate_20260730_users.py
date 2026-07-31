"""Multi-user accounts: `users` table + `sessions.user_id`.

Additive and idempotent. Any pre-roles login stored in the `settings` table is
converted to an admin user by `auth.migrate_legacy_account()` on the next boot,
so no credentials are lost.
"""

import sqlite3
from pathlib import Path

db = Path(__file__).resolve().parent.parent / "spotwebnext.db"
conn = sqlite3.connect(db)

conn.execute(
    """CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY,
        username VARCHAR(64) NOT NULL UNIQUE,
        display_name VARCHAR(64) DEFAULT '',
        password_hash VARCHAR(255) NOT NULL,
        role VARCHAR(16) DEFAULT 'viewer',
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        last_login DATETIME
    )"""
)
conn.execute("CREATE INDEX IF NOT EXISTS ix_users_username ON users(username)")
conn.execute("CREATE INDEX IF NOT EXISTS ix_users_role ON users(role)")

if "user_id" not in {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}:
    conn.execute("ALTER TABLE sessions ADD COLUMN user_id INTEGER")
    print("added sessions.user_id")
conn.execute("CREATE INDEX IF NOT EXISTS ix_sessions_user ON sessions(user_id)")

conn.commit()
print("users rows:", conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])
print(
    "legacy login present:",
    bool(conn.execute("SELECT 1 FROM settings WHERE key='auth_password'").fetchone()),
)
