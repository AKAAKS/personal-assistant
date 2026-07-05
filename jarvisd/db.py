"""SQLite storage for jarvisd — one database, versioned migrations."""

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 2

# version -> SQL script. Later phases append entries and bump SCHEMA_VERSION.
MIGRATIONS = {
    1: "-- baseline: no tables yet; memory/meals/reminders arrive in later phases",
    2: """
        CREATE TABLE messages (
          id INTEGER PRIMARY KEY,
          ts TEXT NOT NULL,
          role TEXT NOT NULL,
          content TEXT NOT NULL
        );
        CREATE VIRTUAL TABLE messages_fts USING fts5(content, content=messages, content_rowid=id);
        CREATE TRIGGER messages_ai AFTER INSERT ON messages BEGIN
          INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
        END;
        CREATE TRIGGER messages_ad AFTER DELETE ON messages BEGIN
          INSERT INTO messages_fts(messages_fts, rowid, content) VALUES ('delete', old.id, old.content);
        END;
    """,
}


def connect(db_path: str = "data/jarvis.db") -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
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
