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
