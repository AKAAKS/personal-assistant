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
