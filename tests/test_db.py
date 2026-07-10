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


def test_migration_reaches_v5():
    conn = db.connect(":memory:")
    row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    assert row["v"] == 5
    assert db.SCHEMA_VERSION == 5
    conn.close()


def test_migration_v5_creates_habit_proposals_table():
    conn = db.connect(":memory:")
    tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "habit_proposals" in tables
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(habit_proposals)")}
    assert {
        "id", "created_ts", "signature", "kind", "payload", "proposal_text", "status",
    } <= cols
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

    conn = db.connect(path)  # runs the v3 -> v4 -> ... -> current hop(s)
    row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    assert row["v"] == db.SCHEMA_VERSION

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


def test_v4_db_upgrades_to_v5(tmp_path):
    path = str(tmp_path / "jarvis.db")
    # Manually build a v4-only database, as if created before phase 5 shipped.
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
    conn.executescript(db.MIGRATIONS[2])
    conn.executescript(db.MIGRATIONS[3])
    conn.executescript(db.MIGRATIONS[4])
    for v in (1, 2, 3, 4):
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (v,))
    conn.execute(
        "INSERT INTO reminders (text, due_ts, recurrence, status, created_ts, condition) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("stretch", "2026-07-05T10:00:00", "daily", "active", "2026-07-01T09:00:00", None),
    )
    conn.execute(
        "INSERT INTO meals (ts, description, quantity, calories, protein_g, carbs_g, fat_g, "
        "confidence, assumptions) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("2026-07-09T08:00:00", "oatmeal", "1 bowl", 250, 8.0, 40.0, 5.0, "high", ""),
    )
    conn.commit()
    # Prove we really are pre-v5: no habit_proposals table yet.
    tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "habit_proposals" not in tables
    conn.close()

    conn = db.connect(path)  # runs only the v4 -> v5 hop
    row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    assert row["v"] == 5

    rem = conn.execute("SELECT * FROM reminders").fetchone()
    assert rem["text"] == "stretch"
    assert rem["due_ts"] == "2026-07-05T10:00:00"
    assert rem["recurrence"] == "daily"
    assert rem["status"] == "active"
    assert rem["created_ts"] == "2026-07-01T09:00:00"
    assert rem["condition"] is None

    meal = conn.execute("SELECT * FROM meals").fetchone()
    assert meal["description"] == "oatmeal"
    assert meal["quantity"] == "1 bowl"
    assert meal["calories"] == 250

    tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "habit_proposals" in tables
    conn.close()
