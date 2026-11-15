import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from jarvisd import db
from jarvisd.reminder_store import ReminderStore, _next_occurrence, parse_recurrence


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


@pytest.mark.parametrize(
    "value,ok",
    [(v, True) for v in ("none", "daily", "weekdays", "weekends", "weekly", "monthly", "monthly:31", "dow:0,2,4", "dow:6")]
    + [(v, False) for v in ("fortnightly", "dow:", "dow:7", "monthly:0", "monthly:32", "dow:1,", "monthly:", "dow:0,2,4x")],
)
def test_set_reminder_schema_pattern_agrees_with_the_parser(value, ok):
    import re
    from jarvisd.tools.reminders import TOOLS
    schema = next(t for t in TOOLS if t["name"] == "set_reminder")
    prop = schema["input_schema"]["properties"]["recurrence"]
    assert "enum" not in prop  # an enum would make dow:/monthly:N unreachable
    assert bool(re.search(prop["pattern"], value)) is ok
    try:
        parse_recurrence(value)
        parsed = True
    except ValueError:
        parsed = False
    assert parsed is ok


def test_set_reminder_description_teaches_the_dow_syntax():
    from jarvisd.tools.reminders import TOOLS
    schema = next(t for t in TOOLS if t["name"] == "set_reminder")
    desc = schema["input_schema"]["properties"]["recurrence"]["description"]
    assert "dow:0,2,4" in desc and "Monday=0" in desc and "weekdays" in desc


def test_set_reminder_handler_stores_a_dow_recurrence(tmp_path):
    from jarvisd.tools import reminders as reminders_tools

    store, conn = make_store(tmp_path)
    handlers = reminders_tools.handlers(store)
    result = handlers["set_reminder"]({"text": "gym", "due": "2026-12-01T07:00:00", "recurrence": "dow:4,0"})
    assert result.startswith("Reminder #")
    assert conn.execute("SELECT recurrence FROM reminders").fetchone()["recurrence"] == "dow:0,4"


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


# --- Task 8 fix round: parameterised recurrence ----------------------------

@pytest.mark.parametrize(
    "value",
    ["none", "daily", "weekdays", "weekends", "weekly", "monthly", "monthly:31", "monthly:1", "dow:0,2,4", "dow:6"],
)
def test_parse_recurrence_accepts_valid_values(value):
    parse_recurrence(value)  # must not raise


@pytest.mark.parametrize(
    "value",
    ["fortnightly", "dow:", "dow:7", "dow:-1", "dow:a", "dow:1,,2", "dow:1.5", "monthly:0", "monthly:32", "monthly:", "monthly:x", "", None, "daily:3", "dow"],
)
def test_parse_recurrence_rejects_invalid_values(value):
    with pytest.raises(ValueError):
        parse_recurrence(value)


def test_dow_is_normalised_sorted_and_deduplicated_on_store(tmp_path):
    store, conn = make_store(tmp_path)
    store.add("x", "2026-12-01T09:00:00", "dow:4,0,2,0")
    assert conn.execute("SELECT recurrence FROM reminders").fetchone()["recurrence"] == "dow:0,2,4"


@pytest.mark.parametrize("bad", ["dow:", "dow:7", "monthly:0", "fortnightly"])
def test_add_rejects_bad_parameterised_recurrence(tmp_path, bad):
    store, _ = make_store(tmp_path)
    with pytest.raises(ValueError):
        store.add("x", "2026-12-01T09:00:00", bad)


def test_add_derives_the_monthly_anchor_from_a_31st_due(tmp_path):
    store, conn = make_store(tmp_path)
    store.add("rent", "2026-01-31T09:00:00", "monthly")
    assert conn.execute("SELECT recurrence FROM reminders").fetchone()["recurrence"] == "monthly:31"


def test_add_keeps_plain_monthly_when_no_clamp_can_happen(tmp_path):
    store, conn = make_store(tmp_path)
    store.add("x", "2026-01-15T09:00:00", "monthly")
    assert conn.execute("SELECT recurrence FROM reminders").fetchone()["recurrence"] == "monthly"


def test_add_accepts_an_explicit_monthly_anchor(tmp_path):
    store, conn = make_store(tmp_path)
    store.add("x", "2026-01-31T09:00:00", "monthly:30")
    assert conn.execute("SELECT recurrence FROM reminders").fetchone()["recurrence"] == "monthly:30"


def test_monthly_31_anchor_returns_to_the_31st_after_february_over_14_months(tmp_path):
    """The ratchet regression: via the store (stateless mark_fired) a 31st
    reminder must be back on the 31st in March, not stuck on the 28th."""
    store, conn = make_store(tmp_path)
    rid = store.add("rent", "2026-01-31T09:00:00", "monthly")
    seen = []
    for _ in range(14):
        due = datetime.fromisoformat(
            conn.execute("SELECT due_ts FROM reminders WHERE id=?", (rid,)).fetchone()["due_ts"]
        )
        store.mark_fired(rid, due)
        row = conn.execute("SELECT due_ts, status FROM reminders WHERE id=?", (rid,)).fetchone()
        assert row["status"] == "active"
        seen.append(row["due_ts"][:10])
    assert seen == [
        "2026-02-28", "2026-03-31", "2026-04-30", "2026-05-31", "2026-06-30",
        "2026-07-31", "2026-08-31", "2026-09-30", "2026-10-31", "2026-11-30",
        "2026-12-31", "2027-01-31", "2027-02-28", "2027-03-31",
    ]


def test_legacy_plain_monthly_row_uses_the_due_days_day_of_month(tmp_path):
    """A pre-existing plain 'monthly' row (no anchor) behaves as before: the
    day-of-month comes from its current due_ts."""
    store, conn = make_store(tmp_path)
    conn.execute(
        "INSERT INTO reminders (text, due_ts, recurrence, status, created_ts) "
        "VALUES ('old', '2026-02-28T09:00:00', 'monthly', 'active', '2026-01-01T00:00:00')"
    )
    conn.commit()
    rid = conn.execute("SELECT id FROM reminders").fetchone()["id"]
    store.mark_fired(rid, datetime(2026, 2, 28, 9, 0))
    row = conn.execute("SELECT due_ts, recurrence FROM reminders").fetchone()
    assert row["due_ts"] == "2026-03-28T09:00:00"
    assert row["recurrence"] == "monthly"


@pytest.mark.parametrize(
    "rec,due",
    [("monthly:31", "2026-01-31T09:00:00"), ("dow:0,2,4", "2026-11-13T09:00:00")],
)
def test_mark_fired_does_not_retire_a_parameterised_row(tmp_path, rec, due):
    store, conn = make_store(tmp_path)
    rid = store.add("x", due, rec)
    store.mark_fired(rid, datetime.fromisoformat(due))
    row = conn.execute("SELECT status, due_ts, recurrence FROM reminders WHERE id=?", (rid,)).fetchone()
    assert row["status"] == "active"
    assert row["due_ts"] > due
    assert row["recurrence"] == rec


def test_stored_parameterised_rows_inserted_directly_survive_mark_fired(tmp_path):
    """Not via add(): rows written straight to the DB must not be treated as unknown."""
    store, conn = make_store(tmp_path)
    for rec in ("monthly:31", "dow:0,2,4"):
        conn.execute(
            "INSERT INTO reminders (text, due_ts, recurrence, status, created_ts) "
            "VALUES (?, '2026-01-31T09:00:00', ?, 'active', '2026-01-01T00:00:00')",
            (rec, rec),
        )
    conn.commit()
    for r in conn.execute("SELECT id FROM reminders").fetchall():
        store.mark_fired(r["id"], datetime(2026, 2, 1))
    assert [r["status"] for r in conn.execute("SELECT status FROM reminders")] == ["active", "active"]


@pytest.mark.parametrize(
    "start,expected",
    [
        (datetime(2026, 11, 9, 7, 30), datetime(2026, 11, 11, 7, 30)),   # Mon -> Wed
        (datetime(2026, 11, 11, 7, 30), datetime(2026, 11, 13, 7, 30)),  # Wed -> Fri
        (datetime(2026, 11, 13, 7, 30), datetime(2026, 11, 16, 7, 30)),  # Fri -> Mon over weekend
        (datetime(2026, 11, 14, 7, 30), datetime(2026, 11, 16, 7, 30)),  # Sat -> Mon
        (datetime(2026, 12, 30, 7, 30), datetime(2027, 1, 1, 7, 30)),    # Wed -> Fri over year end
        (datetime(2026, 12, 31, 7, 30), datetime(2027, 1, 1, 7, 30)),    # Thu -> Fri
    ],
)
def test_dow_mon_wed_fri_picks_the_next_named_weekday_keeping_time_of_day(start, expected):
    assert _next_occurrence(start, "dow:0,2,4", start) == expected


def test_dow_single_day_is_a_week_later():
    mon = datetime(2026, 11, 9, 8, 0)
    assert _next_occurrence(mon, "dow:0", mon) == datetime(2026, 11, 16, 8, 0)


def test_dow_catch_up_lands_on_a_named_weekday_in_the_future():
    now = datetime(2026, 11, 15, 12, 0)  # Sunday
    nxt = _next_occurrence(datetime(2020, 1, 1, 6, 15), "dow:1,3", now)  # Tue/Thu
    assert nxt == datetime(2026, 11, 17, 6, 15)


@pytest.mark.parametrize("bad", ["dow:7", "biweekly", "monthly:0"])
def test_set_reminder_names_the_recurrence_when_the_recurrence_is_invalid(tmp_path, bad):
    from jarvisd.tools import reminders as reminders_tools

    store, conn = make_store(tmp_path)
    result = reminders_tools.handlers(store)["set_reminder"](
        {"text": "gym", "due": "2026-11-16T07:00:00", "recurrence": bad})
    assert result.startswith("Error:") and "recurrence" in result
    assert "not a valid ISO datetime" not in result
    assert conn.execute("SELECT COUNT(*) AS n FROM reminders").fetchone()["n"] == 0


def test_set_reminder_still_names_the_timestamp_when_the_timestamp_is_invalid(tmp_path):
    from jarvisd.tools import reminders as reminders_tools

    store, _ = make_store(tmp_path)
    result = reminders_tools.handlers(store)["set_reminder"](
        {"text": "gym", "due": "next tuesday", "recurrence": "daily"})
    assert "due is not a valid ISO datetime" in result
