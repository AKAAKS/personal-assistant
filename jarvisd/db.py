"""SQLite storage for jarvisd — one database, versioned migrations."""

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 3

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
    3: """
        CREATE TABLE reminders (
          id INTEGER PRIMARY KEY,
          text TEXT NOT NULL,
          due_ts TEXT NOT NULL,
          recurrence TEXT NOT NULL DEFAULT 'none',
          status TEXT NOT NULL DEFAULT 'active',
          created_ts TEXT NOT NULL
        );
        CREATE TABLE notice_queue (
          id INTEGER PRIMARY KEY,
          ts TEXT NOT NULL,
          text TEXT NOT NULL,
          delivered INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE trigger_rules (
          id INTEGER PRIMARY KEY,
          name TEXT NOT NULL,
          event_type TEXT NOT NULL,
          entity_id TEXT,
          to_state TEXT,
          kind TEXT NOT NULL,
          action TEXT NOT NULL,
          cooldown_sec INTEGER NOT NULL DEFAULT 300,
          last_fired_ts TEXT,
          enabled INTEGER NOT NULL DEFAULT 1,
          created_ts TEXT NOT NULL
        );
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
