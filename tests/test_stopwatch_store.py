from datetime import datetime, timedelta, timezone

import pytest

from jarvisd import db
from jarvisd.stopwatch_store import StopwatchStore, elapsed_sec

T0 = datetime(2026, 11, 15, 10, 0, 0)


@pytest.fixture
def conn(tmp_path):
    c = db.connect(str(tmp_path / "t.db"))
    yield c
    c.close()


@pytest.fixture
def store(conn):
    return StopwatchStore(conn)


def test_elapsed_accumulates_across_a_pause(store):
    sid = store.start(now=T0)
    store.pause(sid, T0 + timedelta(seconds=30))
    store.resume(sid, T0 + timedelta(minutes=10))
    assert elapsed_sec(store.get(sid), T0 + timedelta(minutes=10, seconds=15)) == 45


def test_elapsed_sums_the_running_intervals_over_two_pause_cycles(store):
    sid = store.start(now=T0)
    store.pause(sid, T0 + timedelta(seconds=30))              # ran 30
    store.resume(sid, T0 + timedelta(minutes=10))
    store.pause(sid, T0 + timedelta(minutes=10, seconds=20))  # ran 20
    store.resume(sid, T0 + timedelta(hours=2))
    assert elapsed_sec(store.get(sid), T0 + timedelta(hours=2, seconds=7)) == 57


def test_a_paused_stopwatch_does_not_keep_counting(store):
    sid = store.start(now=T0)
    store.pause(sid, T0 + timedelta(seconds=30))
    assert elapsed_sec(store.get(sid), T0 + timedelta(hours=3)) == 30


def test_a_running_stopwatch_counts_wall_time(store):
    sid = store.start(now=T0)
    assert elapsed_sec(store.get(sid), T0 + timedelta(seconds=125)) == 125


def test_stop_returns_the_total_and_retires_it(store):
    sid = store.start(now=T0)
    assert store.stop(sid, T0 + timedelta(seconds=90)) == 90
    assert store.active(T0) == []


def test_stop_marks_the_row_stopped_and_freezes_the_total(store):
    sid = store.start(now=T0)
    store.stop(sid, T0 + timedelta(seconds=90))
    row = store.get(sid)
    assert row["status"] == "stopped"
    assert elapsed_sec(row, T0 + timedelta(days=1)) == 90


def test_stop_a_paused_stopwatch_returns_the_frozen_total(store):
    sid = store.start(now=T0)
    store.pause(sid, T0 + timedelta(seconds=40))
    assert store.stop(sid, T0 + timedelta(hours=1)) == 40


def test_stop_twice_or_unknown_returns_none(store):
    sid = store.start(now=T0)
    store.stop(sid, T0 + timedelta(seconds=5))
    assert store.stop(sid, T0 + timedelta(seconds=9)) is None
    assert store.stop(999, T0) is None


def test_pause_and_resume_reject_the_wrong_state_and_unknown_ids(store):
    sid = store.start(now=T0)
    assert store.resume(sid, T0) is False      # already running
    assert store.pause(sid, T0 + timedelta(seconds=5)) is True
    assert store.pause(sid, T0 + timedelta(seconds=9)) is False  # already paused
    assert elapsed_sec(store.get(sid), T0 + timedelta(minutes=5)) == 5
    assert store.pause(999, T0) is False
    assert store.resume(999, T0) is False


def test_get_returns_none_for_an_unknown_id(store):
    assert store.get(12345) is None


def test_start_stores_the_label_and_local_naive_seconds_timestamp(store):
    sid = store.start("lap", now=T0 + timedelta(microseconds=900))
    row = store.get(sid)
    assert row["label"] == "lap"
    assert row["status"] == "running"
    assert row["started_ts"] == "2026-11-15T10:00:00"


def test_start_with_none_label_stores_empty_string(store):
    assert store.get(store.start(None, now=T0))["label"] == ""


def test_active_lists_running_and_paused_but_not_stopped(store):
    a = store.start("a", now=T0)
    b = store.start("b", now=T0)
    c = store.start("c", now=T0)
    store.pause(b, T0 + timedelta(seconds=1))
    store.stop(c, T0 + timedelta(seconds=1))
    assert [r["id"] for r in store.active(T0)] == [a, b]


def test_negative_elapsed_from_a_backwards_clock_clamps_to_zero(store):
    sid = store.start(now=T0)
    assert elapsed_sec(store.get(sid), T0 - timedelta(minutes=5)) == 0


def test_backwards_clock_at_pause_never_shrinks_accumulated(store):
    sid = store.start(now=T0)
    store.pause(sid, T0 + timedelta(seconds=30))
    store.resume(sid, T0 + timedelta(minutes=1))
    store.pause(sid, T0)  # clock jumped back; must not drop below 30
    assert elapsed_sec(store.get(sid), T0) == 30


def test_aware_now_is_accepted_without_a_type_error(store):
    sid = store.start(now=T0)
    aware = (T0 + timedelta(seconds=10)).astimezone(timezone.utc)
    assert elapsed_sec(store.get(sid), aware) == 10


def test_null_started_ts_raises_value_error_not_type_error():
    row = {"status": "running", "started_ts": None, "accumulated_sec": 0}
    with pytest.raises(ValueError):
        elapsed_sec(row, T0)


def test_active_retires_a_running_row_with_an_unparseable_started_ts(conn, store):
    good = store.start("good", now=T0)
    cur = conn.execute(
        "INSERT INTO stopwatches (label, started_ts, status) VALUES ('bad', 'garbage', 'running')")
    conn.commit()
    bad = cur.lastrowid
    assert [r["id"] for r in store.active(T0 + timedelta(seconds=1))] == [good]
    assert store.get(bad)["status"] == "invalid"
    assert store.get(good)["status"] == "running"


def _insert_bad(conn):
    cur = conn.execute(
        "INSERT INTO stopwatches (label, started_ts, status) VALUES ('bad', 'garbage', 'running')")
    conn.commit()
    return cur.lastrowid


def test_pause_and_stop_on_an_unparseable_row_retire_it_and_report_failure(conn, store):
    a, b = _insert_bad(conn), _insert_bad(conn)
    assert store.pause(a, T0) is False
    assert store.get(a)["status"] == "invalid"
    assert store.stop(b, T0) is None
    assert store.get(b)["status"] == "invalid"


def test_pause_stop_resume_on_an_invalid_row_do_nothing(conn, store):
    bad = _insert_bad(conn)
    store.active(T0)  # retires it
    assert store.pause(bad, T0) is False
    assert store.resume(bad, T0) is False
    assert store.stop(bad, T0) is None
    assert store.get(bad)["status"] == "invalid"


def test_an_absurd_aware_now_raises_and_does_not_retire_live_rows(store):
    sid = store.start(now=T0)
    bad_now = datetime.min.replace(tzinfo=timezone.utc)
    with pytest.raises((OSError, OverflowError, ValueError)):
        store.active(bad_now)
    with pytest.raises((OSError, OverflowError, ValueError)):
        store.pause(sid, bad_now)
    assert store.get(sid)["status"] == "running"
