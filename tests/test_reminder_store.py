import sqlite3
from datetime import datetime, timedelta

import pytest

from jarvisd import db
from jarvisd.reminder_store import ReminderStore


def make_store(tmp_path):
    conn = db.connect(str(tmp_path / "jarvis.db"))
    return ReminderStore(conn), conn


def test_migration_reaches_v3_and_is_idempotent(tmp_path):
    path = str(tmp_path / "jarvis.db")
    db.connect(path).close()
    conn = db.connect(path)  # must not re-run migrations / raise
    row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    assert row["v"] == 3
    assert db.SCHEMA_VERSION == 3
    conn.close()


def test_v2_db_upgrades_to_v3_preserving_data(tmp_path):
    path = str(tmp_path / "jarvis.db")
    # Manually build a v2-only database, as if created before phase 3 shipped.
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
    conn.executescript(db.MIGRATIONS[2])
    conn.execute("INSERT INTO schema_version (version) VALUES (1)")
    conn.execute("INSERT INTO schema_version (version) VALUES (2)")
    conn.execute(
        "INSERT INTO messages (ts, role, content) VALUES (?, ?, ?)",
        ("2026-01-01T00:00:00", "user", "hello before upgrade"),
    )
    conn.commit()
    conn.close()

    conn = db.connect(path)
    row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    assert row["v"] == 3

    msg = conn.execute("SELECT content FROM messages").fetchone()
    assert msg["content"] == "hello before upgrade"

    tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"reminders", "notice_queue", "trigger_rules"} <= tables
    conn.close()


def test_add_returns_id_and_stores_defaults(tmp_path):
    store, conn = make_store(tmp_path)
    rid = store.add("stretch", "2026-07-05T10:00:00")
    assert isinstance(rid, int)
    row = conn.execute("SELECT * FROM reminders WHERE id=?", (rid,)).fetchone()
    assert row["text"] == "stretch"
    assert row["due_ts"] == "2026-07-05T10:00:00"
    assert row["recurrence"] == "none"
    assert row["status"] == "active"
    assert row["created_ts"]


def test_add_stores_recurrence(tmp_path):
    store, conn = make_store(tmp_path)
    rid = store.add("standup", "2026-07-05T09:00:00", recurrence="daily")
    row = conn.execute("SELECT recurrence FROM reminders WHERE id=?", (rid,)).fetchone()
    assert row["recurrence"] == "daily"


def test_add_rejects_bad_recurrence(tmp_path):
    store, conn = make_store(tmp_path)
    with pytest.raises(ValueError):
        store.add("x", "2026-07-05T10:00:00", recurrence="monthly")


def test_add_rejects_unparseable_due_ts(tmp_path):
    store, conn = make_store(tmp_path)
    with pytest.raises(ValueError):
        store.add("x", "not-a-date")


def test_list_active_ordered_by_due_ts(tmp_path):
    store, conn = make_store(tmp_path)
    store.add("later", "2026-07-06T10:00:00")
    store.add("earlier", "2026-07-05T10:00:00")
    rows = store.list_active()
    assert [r["text"] for r in rows] == ["earlier", "later"]


def test_list_active_excludes_cancelled(tmp_path):
    store, conn = make_store(tmp_path)
    rid = store.add("x", "2026-07-05T10:00:00")
    store.cancel(rid)
    assert store.list_active() == []


def test_cancel_returns_true_and_sets_status(tmp_path):
    store, conn = make_store(tmp_path)
    rid = store.add("x", "2026-07-05T10:00:00")
    assert store.cancel(rid) is True
    row = conn.execute("SELECT status FROM reminders WHERE id=?", (rid,)).fetchone()
    assert row["status"] == "cancelled"


def test_cancel_returns_false_for_missing_id(tmp_path):
    store, conn = make_store(tmp_path)
    assert store.cancel(999) is False


def test_cancel_returns_false_for_already_cancelled(tmp_path):
    store, conn = make_store(tmp_path)
    rid = store.add("x", "2026-07-05T10:00:00")
    store.cancel(rid)
    assert store.cancel(rid) is False


def test_due_boundary_equal_now_fires(tmp_path):
    store, conn = make_store(tmp_path)
    now = datetime(2026, 7, 5, 10, 0, 0)
    store.add("x", now.isoformat(timespec="seconds"))
    rows = store.due(now)
    assert len(rows) == 1


def test_due_excludes_future(tmp_path):
    store, conn = make_store(tmp_path)
    now = datetime(2026, 7, 5, 10, 0, 0)
    store.add("x", (now + timedelta(minutes=1)).isoformat(timespec="seconds"))
    assert store.due(now) == []


def test_due_excludes_cancelled(tmp_path):
    store, conn = make_store(tmp_path)
    now = datetime(2026, 7, 5, 10, 0, 0)
    rid = store.add("x", now.isoformat(timespec="seconds"))
    store.cancel(rid)
    assert store.due(now) == []


def test_mark_fired_none_recurrence_marks_done(tmp_path):
    store, conn = make_store(tmp_path)
    now = datetime(2026, 7, 5, 10, 0, 0)
    rid = store.add("x", now.isoformat(timespec="seconds"))
    store.mark_fired(rid, now)
    row = conn.execute("SELECT status, due_ts FROM reminders WHERE id=?", (rid,)).fetchone()
    assert row["status"] == "done"
    assert row["due_ts"] == "2026-07-05T10:00:00"  # untouched on terminal recurrence


def test_mark_fired_daily_advances_one_day_when_fired_on_time(tmp_path):
    store, conn = make_store(tmp_path)
    due = datetime(2026, 7, 5, 9, 30, 0)
    rid = store.add("x", due.isoformat(timespec="seconds"), recurrence="daily")
    store.mark_fired(rid, due)
    row = conn.execute("SELECT due_ts, status FROM reminders WHERE id=?", (rid,)).fetchone()
    assert row["due_ts"] == "2026-07-06T09:30:00"
    assert row["status"] == "active"


def test_mark_fired_daily_catches_up_missed_days_fires_once(tmp_path):
    store, conn = make_store(tmp_path)
    due = datetime(2026, 7, 1, 9, 30, 0)
    now = datetime(2026, 7, 4, 12, 0, 0)  # missed 3 days
    rid = store.add("x", due.isoformat(timespec="seconds"), recurrence="daily")
    store.mark_fired(rid, now)
    row = conn.execute("SELECT due_ts FROM reminders WHERE id=?", (rid,)).fetchone()
    assert row["due_ts"] == "2026-07-05T09:30:00"


def test_mark_fired_weekly_advances_seven_days(tmp_path):
    store, conn = make_store(tmp_path)
    due = datetime(2026, 7, 5, 8, 0, 0)
    rid = store.add("x", due.isoformat(timespec="seconds"), recurrence="weekly")
    store.mark_fired(rid, due)
    row = conn.execute("SELECT due_ts FROM reminders WHERE id=?", (rid,)).fetchone()
    assert row["due_ts"] == "2026-07-12T08:00:00"


def test_mark_fired_weekly_catches_up_missed_weeks_fires_once(tmp_path):
    store, conn = make_store(tmp_path)
    due = datetime(2026, 6, 1, 8, 0, 0)
    now = datetime(2026, 7, 5, 9, 0, 0)  # multiple weeks late
    rid = store.add("x", due.isoformat(timespec="seconds"), recurrence="weekly")
    store.mark_fired(rid, now)
    row = conn.execute("SELECT due_ts FROM reminders WHERE id=?", (rid,)).fetchone()
    assert row["due_ts"] == "2026-07-06T08:00:00"
    assert datetime.fromisoformat(row["due_ts"]) > now


def test_mark_fired_missing_id_is_noop(tmp_path):
    store, conn = make_store(tmp_path)
    store.mark_fired(999, datetime(2026, 7, 5, 10, 0, 0))  # must not raise


def test_mark_fired_cancelled_none_reminder_stays_cancelled(tmp_path):
    store, conn = make_store(tmp_path)
    rid = store.add("x", "2026-07-05T10:00:00")
    store.cancel(rid)
    store.mark_fired(rid, datetime(2026, 7, 5, 10, 0, 0))
    row = conn.execute("SELECT status, due_ts FROM reminders WHERE id=?", (rid,)).fetchone()
    assert row["status"] == "cancelled"
    assert row["due_ts"] == "2026-07-05T10:00:00"


def test_mark_fired_cancelled_daily_reminder_untouched(tmp_path):
    store, conn = make_store(tmp_path)
    rid = store.add("x", "2026-07-05T09:30:00", recurrence="daily")
    store.cancel(rid)
    store.mark_fired(rid, datetime(2026, 7, 8, 12, 0, 0))
    row = conn.execute("SELECT status, due_ts FROM reminders WHERE id=?", (rid,)).fetchone()
    assert row["status"] == "cancelled"
    assert row["due_ts"] == "2026-07-05T09:30:00"
