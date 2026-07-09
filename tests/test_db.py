import sqlite3
from jarvisd import db


def test_connect_creates_file_and_schema(tmp_path):
    path = tmp_path / "sub" / "jarvis.db"
    conn = db.connect(str(path))
    assert path.exists()
    row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    assert row["v"] == db.SCHEMA_VERSION
    conn.close()


def test_connect_is_idempotent(tmp_path):
    path = str(tmp_path / "jarvis.db")
    db.connect(path).close()
    conn = db.connect(path)  # must not re-run migrations / raise
    count = conn.execute("SELECT COUNT(*) AS c FROM schema_version").fetchone()["c"]
    assert count == db.SCHEMA_VERSION
    conn.close()


def test_migration_v4_creates_meals_table(tmp_path):
    conn = db.connect(str(tmp_path / "jarvis.db"))
    tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "meals" in tables
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(meals)")}
    assert {
        "id", "ts", "description", "quantity", "calories",
        "protein_g", "carbs_g", "fat_g", "confidence", "assumptions",
    } <= cols
    conn.close()


def test_migration_v4_adds_reminders_condition_column(tmp_path):
    conn = db.connect(str(tmp_path / "jarvis.db"))
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(reminders)")}
    assert "condition" in cols
    conn.close()


def test_migration_reaches_v4():
    conn = db.connect(":memory:")
    row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    assert row["v"] == 4
    assert db.SCHEMA_VERSION == 4
    conn.close()


def test_v3_db_upgrades_to_v4_preserving_reminder_data(tmp_path):
    path = str(tmp_path / "jarvis.db")
    # Manually build a v3-only database, as if created before phase 4 shipped.
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
    conn.executescript(db.MIGRATIONS[2])
    conn.executescript(db.MIGRATIONS[3])
    for v in (1, 2, 3):
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (v,))
    conn.execute(
        "INSERT INTO reminders (text, due_ts, recurrence, status, created_ts) "
        "VALUES (?, ?, ?, ?, ?)",
        ("stretch", "2026-07-05T10:00:00", "daily", "active", "2026-07-01T09:00:00"),
    )
    conn.commit()
    # Prove we really are pre-v4: no condition column, no meals table yet.
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(reminders)")}
    assert "condition" not in cols
    tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "meals" not in tables
    conn.close()

    conn = db.connect(path)  # runs only the v3 -> v4 hop
    row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    assert row["v"] == 4

    rem = conn.execute("SELECT * FROM reminders").fetchone()
    assert rem["text"] == "stretch"
    assert rem["due_ts"] == "2026-07-05T10:00:00"
    assert rem["recurrence"] == "daily"
    assert rem["status"] == "active"
    assert rem["created_ts"] == "2026-07-01T09:00:00"
    assert rem["condition"] is None

    tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "meals" in tables
    conn.close()
