"""SQLite storage for jarvisd — one database, versioned migrations."""

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1

# version -> SQL script. Later phases append entries and bump SCHEMA_VERSION.
MIGRATIONS = {
    1: "-- baseline: no tables yet; memory/meals/reminders arrive in later phases",
}


def connect(db_path: str = "data/jarvis.db") -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
    row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    current = row["v"] or 0
    for version in range(current + 1, SCHEMA_VERSION + 1):
        conn.executescript(MIGRATIONS[version])
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
    conn.commit()
