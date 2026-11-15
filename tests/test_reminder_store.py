import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from jarvisd import db
from jarvisd.reminder_store import ReminderStore, _next_occurrence


def make_store(tmp_path):
    conn = db.connect(str(tmp_path / "jarvis.db"))
    return ReminderStore(conn), conn


def test_migration_reaches_current_version_and_is_idempotent(tmp_path):
    path = str(tmp_path / "jarvis.db")
    db.connect(path).close()
    conn = db.connect(path)  # must not re-run migrations / raise
    row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    assert row["v"] == db.SCHEMA_VERSION
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
    assert row["v"] == db.SCHEMA_VERSION

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
        store.add("x", "2026-07-05T10:00:00", recurrence="fortnightly")


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


def test_add_stores_condition(tmp_path):
    store, conn = make_store(tmp_path)
    rid = store.add(
        "log dinner", "2026-07-05T20:30:00", condition="meal_not_logged_today"
    )
    row = conn.execute("SELECT condition FROM reminders WHERE id=?", (rid,)).fetchone()
    assert row["condition"] == "meal_not_logged_today"


def test_add_default_condition_is_none(tmp_path):
    store, conn = make_store(tmp_path)
    rid = store.add("x", "2026-07-05T10:00:00")
    row = conn.execute("SELECT condition FROM reminders WHERE id=?", (rid,)).fetchone()
    assert row["condition"] is None


def test_add_normalizes_offset_aware_due_ts_to_local_naive(tmp_path):
    """An offset-aware due_ts (the model is told the local zone with %Z, so it
    can easily send one) must be stored as local naive time. Issue #2: a single
    aware row made due() raise TypeError, which killed every reminder."""
    store, conn = make_store(tmp_path)
    aware = datetime(2026, 1, 1, 8, 0, tzinfo=timezone(timedelta(hours=-5)))
    rid = store.add("aware", aware.isoformat())

    stored = conn.execute("SELECT due_ts FROM reminders WHERE id = ?", (rid,)).fetchone()["due_ts"]
    assert datetime.fromisoformat(stored).tzinfo is None
    assert datetime.fromisoformat(stored) == aware.astimezone().replace(tzinfo=None)


def test_add_accepts_z_suffix_due_ts(tmp_path):
    store, conn = make_store(tmp_path)
    rid = store.add("zulu", "2026-01-01T12:00:00Z")
    stored = conn.execute("SELECT due_ts FROM reminders WHERE id = ?", (rid,)).fetchone()["due_ts"]
    assert datetime.fromisoformat(stored).tzinfo is None
    expected = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc).astimezone().replace(tzinfo=None)
    assert datetime.fromisoformat(stored) == expected


def test_due_returns_mixed_aware_and_naive_rows(tmp_path):
    """Issue #2 repro: an aware row alongside a naive one must not break the scan."""
    store, _ = make_store(tmp_path)
    store.add("a", "2026-01-01T08:00:00-05:00")
    store.add("b", "2026-01-01T08:00")

    rows = store.due(datetime(2026, 1, 2))
    assert sorted(r["text"] for r in rows) == ["a", "b"]


def test_due_reads_legacy_aware_row_written_before_the_fix(tmp_path):
    """Rows an older build already stored in an aware format are normalized on
    read, so an existing database keeps firing after the upgrade."""
    store, conn = make_store(tmp_path)
    store.add("good", "2026-01-01T08:00")
    conn.execute(
        "INSERT INTO reminders (text, due_ts, recurrence, status, created_ts) "
        "VALUES ('legacy', '2026-01-01T08:00:00-05:00', 'none', 'active', '2026-01-01T00:00:00')"
    )
    conn.commit()

    rows = store.due(datetime(2026, 1, 2))
    assert sorted(r["text"] for r in rows) == ["good", "legacy"]


def test_due_skips_unusable_row_instead_of_failing(tmp_path):
    """Second guard: one junk due_ts must not abort the scan and stop every
    other reminder from firing."""
    store, conn = make_store(tmp_path)
    store.add("good", "2026-01-01T08:00")
    conn.execute(
        "INSERT INTO reminders (text, due_ts, recurrence, status, created_ts) "
        "VALUES ('junk', 'tomorrow morning', 'none', 'active', '2026-01-01T00:00:00')"
    )
    conn.commit()

    rows = store.due(datetime(2026, 1, 2))
    assert [r["text"] for r in rows] == ["good"]


def test_list_active_sorts_mixed_formats_chronologically(tmp_path):
    """due_ts is sorted as a string in SQL, so normalizing at write time is what
    keeps the order right."""
    store, _ = make_store(tmp_path)
    store.add("later", "2026-01-01T23:00")
    store.add("earlier", "2026-01-01T09:00:00+00:00")

    texts = [r["text"] for r in store.list_active()]
    naive_earlier = datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc).astimezone().replace(tzinfo=None)
    expected = ["earlier", "later"] if naive_earlier.hour < 23 else ["later", "earlier"]
    assert texts == expected


def test_mark_fired_recurring_aware_row_reschedules(tmp_path):
    """mark_fired compares due_ts against a naive now too -- normalizing in
    add() has to cover the recurrence path as well."""
    store, conn = make_store(tmp_path)
    rid = store.add("daily", "2026-01-01T08:00:00-05:00", recurrence="daily")
    store.mark_fired(rid, datetime(2026, 1, 3, 12, 0))

    row = conn.execute("SELECT due_ts, status FROM reminders WHERE id = ?", (rid,)).fetchone()
    assert row["status"] == "active"
    assert datetime.fromisoformat(row["due_ts"]) > datetime(2026, 1, 3, 12, 0)


def test_normalize_due_ts_preserves_the_instant(tmp_path):
    """Stronger than comparing against astimezone() (which is tautological):
    re-localizing the stored naive value must give back the same instant."""
    store, conn = make_store(tmp_path)
    aware = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    rid = store.add("utc noon", aware.isoformat())

    stored = conn.execute("SELECT due_ts FROM reminders WHERE id = ?", (rid,)).fetchone()["due_ts"]
    naive = datetime.fromisoformat(stored)
    assert naive.tzinfo is None
    assert naive.astimezone() == aware  # same moment, expressed locally


def test_normalize_due_ts_is_idempotent(tmp_path):
    from jarvisd.reminder_store import normalize_due_ts
    once = normalize_due_ts("2026-01-01T12:00:00+00:00")
    assert normalize_due_ts(once) == once


def test_add_rejects_out_of_range_aware_due_ts(tmp_path):
    """On Windows astimezone() raises OSError, not ValueError, for an aware
    value outside the platform range -- add() must still report it friendly."""
    store, _ = make_store(tmp_path)
    with pytest.raises(ValueError, match="not a valid ISO datetime"):
        store.add("ancient", "1950-06-01T12:00:00Z")


def test_due_retires_out_of_range_row_and_keeps_scanning(tmp_path):
    """The OSError case the first guard missed: one such row must not abort
    the scan, and must stop warning on every 15s tick."""
    store, conn = make_store(tmp_path)
    store.add("good", "2026-01-01T08:00")
    conn.execute(
        "INSERT INTO reminders (text, due_ts, recurrence, status, created_ts) "
        "VALUES ('ancient', '1950-06-01T12:00:00Z', 'none', 'active', '2026-01-01T00:00:00')"
    )
    conn.commit()

    assert [r["text"] for r in store.due(datetime(2026, 1, 2))] == ["good"]
    status = conn.execute("SELECT status FROM reminders WHERE text = 'ancient'").fetchone()["status"]
    assert status == "invalid"
    # Retired, so the next tick neither warns nor rescans it.
    assert [r["text"] for r in store.due(datetime(2026, 1, 2))] == ["good"]


def test_mark_fired_tolerates_unusable_due_ts(tmp_path):
    """mark_fired is public API and runs inside the tick's to_thread -- an
    unparseable recurring row must not raise out of it."""
    store, conn = make_store(tmp_path)
    conn.execute(
        "INSERT INTO reminders (text, due_ts, recurrence, status, created_ts) "
        "VALUES ('junk', 'tomorrow morning', 'daily', 'active', '2026-01-01T00:00:00')"
    )
    conn.commit()
    rid = conn.execute("SELECT id FROM reminders WHERE text = 'junk'").fetchone()["id"]

    store.mark_fired(rid, datetime(2026, 1, 2))  # must not raise

    assert conn.execute(
        "SELECT status FROM reminders WHERE id = ?", (rid,)
    ).fetchone()["status"] == "invalid"


def test_set_reminder_echoes_the_normalized_time(tmp_path):
    """Issue #2 follow-up: the confirmation must name the time that will fire,
    not the offset-aware string the model sent."""
    from jarvisd.tools import reminders as reminders_tools

    store, _ = make_store(tmp_path)
    handlers = reminders_tools.handlers(store)
    result = handlers["set_reminder"]({"text": "standup", "due": "2026-01-01T12:00:00Z"})

    expected = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc).astimezone().replace(tzinfo=None)
    assert expected.isoformat(timespec="seconds") in result
    assert "+00:00" not in result and "Z" not in result


# --- Task 8: extended recurrence -------------------------------------------

FRI = datetime(2026, 11, 13, 9, 0)  # Friday
SAT = datetime(2026, 11, 14, 9, 0)  # Saturday


def test_weekdays_skips_the_weekend():
    assert _next_occurrence(FRI, "weekdays", FRI) == datetime(2026, 11, 16, 9, 0)


def test_weekdays_created_on_a_saturday_lands_on_monday():
    assert _next_occurrence(SAT, "weekdays", SAT) == datetime(2026, 11, 16, 9, 0)


def test_weekdays_midweek_advances_exactly_one_day():
    wed = datetime(2026, 11, 11, 9, 0)
    assert _next_occurrence(wed, "weekdays", wed) == datetime(2026, 11, 12, 9, 0)


def test_weekends_from_friday_and_saturday():
    assert _next_occurrence(SAT, "weekends", SAT) == datetime(2026, 11, 15, 9, 0)
    assert _next_occurrence(FRI, "weekends", FRI) == datetime(2026, 11, 14, 9, 0)


def test_weekends_from_sunday_skips_the_working_week():
    sun = datetime(2026, 11, 15, 9, 0)
    assert _next_occurrence(sun, "weekends", sun) == datetime(2026, 11, 21, 9, 0)


def test_monthly_advances_one_month():
    d = datetime(2026, 1, 15, 9, 0)
    assert _next_occurrence(d, "monthly", d) == datetime(2026, 2, 15, 9, 0)


def test_monthly_on_the_31st_clamps_to_the_last_day_of_february():
    d = datetime(2026, 1, 31, 9, 0)
    assert _next_occurrence(d, "monthly", d) == datetime(2026, 2, 28, 9, 0)


def test_monthly_on_the_29th_of_february_in_a_leap_year():
    d = datetime(2028, 2, 29, 9, 0)
    # next month has 31 days: the 29th exists, no clamp
    assert _next_occurrence(d, "monthly", d) == datetime(2028, 3, 29, 9, 0)
    # a 31st anchor clamps to the leap-year 29th
    jan = datetime(2028, 1, 31, 9, 0)
    assert _next_occurrence(jan, "monthly", jan) == datetime(2028, 2, 29, 9, 0)


def test_monthly_from_march_31_clamps_to_april_30():
    d = datetime(2026, 3, 31, 9, 0)
    assert _next_occurrence(d, "monthly", d) == datetime(2026, 4, 30, 9, 0)


def test_monthly_rolls_the_year():
    d = datetime(2026, 12, 15, 9, 0)
    assert _next_occurrence(d, "monthly", d) == datetime(2027, 1, 15, 9, 0)


def test_monthly_with_a_31_anchor_never_drifts_over_a_year():
    """Advancing repeatedly with the original anchor day returns to the 31st
    in every 31-day month instead of sticking on the clamped 28th."""
    cur = datetime(2026, 1, 31, 9, 0)
    seen = []
    for _ in range(12):
        cur = _next_occurrence(cur, "monthly", cur, anchor_day=31)
        seen.append((cur.year, cur.month, cur.day))
    assert seen == [
        (2026, 2, 28), (2026, 3, 31), (2026, 4, 30), (2026, 5, 31),
        (2026, 6, 30), (2026, 7, 31), (2026, 8, 31), (2026, 9, 30),
        (2026, 10, 31), (2026, 11, 30), (2026, 12, 31), (2027, 1, 31),
    ]


def test_monthly_catch_up_computes_from_the_original_due_not_cumulatively():
    past = datetime(2026, 1, 31, 9, 0)
    now = datetime(2026, 3, 15, 12, 0)
    assert _next_occurrence(past, "monthly", now) == datetime(2026, 3, 31, 9, 0)


def test_every_recurrence_terminates_and_lands_strictly_after_now():
    now = datetime(2026, 11, 15, 12, 0)
    past = datetime(2020, 1, 1, 9, 0)
    for rec in ("daily", "weekdays", "weekends", "weekly", "monthly"):
        nxt = _next_occurrence(past, rec, now)
        assert nxt > now, rec
        assert nxt.time() == past.time(), rec  # wall-clock time-of-day preserved


def test_catch_up_lands_on_the_correct_weekday_not_just_the_future():
    # Sat 2026-11-14 is "now" at noon; weekdays catch-up must pick Mon the 16th.
    now = datetime(2026, 11, 14, 12, 0)
    past = datetime(2026, 10, 1, 9, 0)
    assert _next_occurrence(past, "weekdays", now) == datetime(2026, 11, 16, 9, 0)
    assert _next_occurrence(past, "weekends", now) == datetime(2026, 11, 15, 9, 0)


def test_wall_clock_time_survives_a_dst_boundary():
    # US clocks fall back 2026-11-01; a naive 07:00 reminder must stay at 07:00.
    d = datetime(2026, 10, 31, 7, 0)
    assert _next_occurrence(d, "daily", d) == datetime(2026, 11, 1, 7, 0)
    assert _next_occurrence(d, "weekly", d) == datetime(2026, 11, 7, 7, 0)


def test_next_occurrence_rejects_an_unknown_recurrence():
    with pytest.raises(ValueError):
        _next_occurrence(FRI, "fortnightly", FRI)
    with pytest.raises(ValueError):
        _next_occurrence(FRI, "none", FRI)


def test_set_reminder_schema_offers_the_new_recurrences():
    from jarvisd.tools.reminders import TOOLS
    schema = next(t for t in TOOLS if t["name"] == "set_reminder")
    enum = schema["input_schema"]["properties"]["recurrence"]["enum"]
    assert set(enum) == {"none", "daily", "weekdays", "weekends", "weekly", "monthly"}


def test_an_invalid_recurrence_is_still_rejected(tmp_path):
    store, _ = make_store(tmp_path)
    with pytest.raises(ValueError):
        store.add("x", "2026-12-01T09:00:00", "fortnightly")


def test_set_reminder_tolerates_an_explicit_null_recurrence(tmp_path):
    from jarvisd.tools import reminders as reminders_tools

    store, conn = make_store(tmp_path)
    handlers = reminders_tools.handlers(store)
    result = handlers["set_reminder"](
        {"text": "x", "due": "2026-12-01T09:00:00", "recurrence": None}
    )
    assert result.startswith("Reminder #")
    assert conn.execute("SELECT recurrence FROM reminders").fetchone()["recurrence"] == "none"


@pytest.mark.parametrize(
    "rec,due,now,expected",
    [
        ("weekdays", datetime(2026, 11, 13, 9, 0), datetime(2026, 11, 13, 9, 0), "2026-11-16T09:00:00"),
        ("weekends", datetime(2026, 11, 13, 9, 0), datetime(2026, 11, 13, 9, 0), "2026-11-14T09:00:00"),
        ("monthly", datetime(2026, 1, 31, 9, 0), datetime(2026, 1, 31, 9, 0), "2026-02-28T09:00:00"),
    ],
)
def test_mark_fired_applies_the_new_recurrences(tmp_path, rec, due, now, expected):
    store, conn = make_store(tmp_path)
    rid = store.add("x", due.isoformat(timespec="seconds"), recurrence=rec)
    store.mark_fired(rid, now)
    row = conn.execute("SELECT due_ts, status FROM reminders WHERE id=?", (rid,)).fetchone()
    assert (row["due_ts"], row["status"]) == (expected, "active")


def test_mark_fired_retires_a_row_with_an_unknown_recurrence(tmp_path):
    """A hand-edited/legacy recurrence must not raise out of the tick, and must
    not silently behave like 'none' (done) either."""
    store, conn = make_store(tmp_path)
    conn.execute(
        "INSERT INTO reminders (text, due_ts, recurrence, status, created_ts) "
        "VALUES ('odd', '2026-01-01T09:00:00', 'fortnightly', 'active', '2026-01-01T00:00:00')"
    )
    conn.commit()
    rid = conn.execute("SELECT id FROM reminders").fetchone()["id"]
    store.mark_fired(rid, datetime(2026, 1, 2))  # must not raise
    assert conn.execute("SELECT status FROM reminders").fetchone()["status"] == "invalid"
