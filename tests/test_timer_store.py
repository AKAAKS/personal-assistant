import pytest
from datetime import datetime, timedelta

from jarvisd import db
from jarvisd.timer_store import TimerStore


@pytest.fixture
def conn(tmp_path):
    c = db.connect(str(tmp_path / "t.db"))
    yield c
    c.close()


@pytest.fixture
def store(conn):
    return TimerStore(conn)

T0 = datetime(2026, 11, 15, 10, 0, 0)


def test_add_sets_due_and_running(store):
    tid = store.add(600, "tea", now=T0)
    row = store.get(tid)
    assert row["status"] == "running"
    assert row["label"] == "tea"
    assert row["due_ts"] == "2026-11-15T10:10:00"
    assert row["remaining_sec"] is None


def test_remaining_counts_down(store):
    tid = store.add(600, now=T0)
    from jarvisd.timer_store import remaining_for
    assert remaining_for(store.get(tid), T0 + timedelta(minutes=4)) == 360


def test_remaining_never_goes_negative(store):
    tid = store.add(60, now=T0)
    from jarvisd.timer_store import remaining_for
    assert remaining_for(store.get(tid), T0 + timedelta(hours=2)) == 0


def test_pause_freezes_remaining_and_clears_due(store):
    # A paused timer must never come due; a stale due_ts would fire instantly.
    tid = store.add(600, now=T0)
    assert store.pause(tid, T0 + timedelta(minutes=2)) is True
    row = store.get(tid)
    assert row["status"] == "paused"
    assert row["remaining_sec"] == 480
    assert row["due_ts"] is None
    assert store.due(T0 + timedelta(hours=1)) == []


def test_resume_restores_the_frozen_remainder(store):
    tid = store.add(600, now=T0)
    store.pause(tid, T0 + timedelta(minutes=2))
    store.resume(tid, T0 + timedelta(hours=5))      # paused for hours
    row = store.get(tid)
    assert row["status"] == "running"
    assert row["due_ts"] == "2026-11-15T15:08:00"   # 8 min from resume
    assert row["remaining_sec"] is None


def test_pause_is_rejected_for_a_timer_that_is_not_running(store):
    tid = store.add(600, now=T0)
    store.cancel(tid)
    assert store.pause(tid, T0) is False


def test_extend_pushes_due_out_while_running(store):
    tid = store.add(600, now=T0)
    store.extend(tid, 300, T0)
    assert store.get(tid)["due_ts"] == "2026-11-15T10:15:00"


def test_extend_adds_to_the_remainder_while_paused(store):
    tid = store.add(600, now=T0)
    store.pause(tid, T0 + timedelta(minutes=2))
    store.extend(tid, 300, T0)
    assert store.get(tid)["remaining_sec"] == 780


def test_due_returns_only_elapsed_running_timers(store):
    a = store.add(60, now=T0)
    store.add(6000, now=T0)
    assert [r["id"] for r in store.due(T0 + timedelta(minutes=5))] == [a]


def test_a_timer_due_during_downtime_still_fires_after_restart(store):
    # The daemon was off when it came due; it must not be lost.
    tid = store.add(60, now=T0)
    assert [r["id"] for r in store.due(T0 + timedelta(days=1))] == [tid]


def test_mark_ringing_then_dismiss(store):
    tid = store.add(60, now=T0)
    store.mark_ringing(tid, T0 + timedelta(minutes=1))
    assert store.get(tid)["status"] == "ringing"
    assert store.get(tid)["fired_ts"] == "2026-11-15T10:01:00"
    assert store.dismiss(tid) is True
    assert store.get(tid)["status"] == "done"


def test_dismiss_is_idempotent(store):
    tid = store.add(60, now=T0)
    store.dismiss(tid)
    assert store.dismiss(tid) is False          # no longer live, but no raise


def test_snooze_puts_a_ringing_timer_back_to_running(store):
    tid = store.add(60, now=T0)
    store.mark_ringing(tid, T0 + timedelta(minutes=1))
    store.snooze(tid, 540, T0 + timedelta(minutes=1))
    row = store.get(tid)
    assert row["status"] == "running"
    assert row["due_ts"] == "2026-11-15T10:10:00"


def test_recently_fired_surfaces_a_done_timer(store):
    # "Did my timer go off?" was unanswerable: done rows were invisible.
    tid = store.add(60, "pasta", now=T0)
    store.mark_ringing(tid, T0 + timedelta(minutes=1))
    store.dismiss(tid)
    assert [r["id"] for r in store.recently_fired(T0 + timedelta(minutes=10))] == [tid]
    assert store.recently_fired(T0 + timedelta(hours=5)) == []


def test_find_by_label_is_case_insensitive_and_returns_all_matches(store):
    store.add(60, "Tea", now=T0)
    store.add(60, "tea", now=T0)
    assert len(store.find_by_label("TEA")) == 2


def test_list_live_is_soonest_first_and_excludes_finished(store):
    far = store.add(6000, now=T0)
    soon = store.add(60, now=T0)
    gone = store.add(60, now=T0)
    store.cancel(gone)
    assert [r["id"] for r in store.list_live(T0)] == [soon, far]


def test_an_unparseable_due_ts_does_not_abort_the_scan(store, conn):
    good = store.add(60, now=T0)
    conn.execute("INSERT INTO timers (duration_sec, due_ts, status, created_ts) "
                 "VALUES (60, 'not-a-date', 'running', '2026-11-15T10:00:00')")
    conn.commit()
    assert [r["id"] for r in store.due(T0 + timedelta(minutes=5))] == [good]

def test_a_running_timer_with_null_due_ts_is_retired_not_raised(store, conn):
    # due_ts is NULL only while paused; a running row with NULL must not
    # raise TypeError out of fromisoformat inside the tick.
    conn.execute("INSERT INTO timers (duration_sec, due_ts, status, created_ts) "
                 "VALUES (60, NULL, 'running', '2026-11-15T10:00:00')")
    conn.commit()
    assert store.due(T0) == []
    assert store.list_live(T0) == []
    assert conn.execute("SELECT status FROM timers").fetchone()["status"] == "invalid"


def test_bad_row_is_retired_so_it_warns_only_once(store, conn, caplog):
    conn.execute("INSERT INTO timers (duration_sec, due_ts, status, created_ts) "
                 "VALUES (60, 'not-a-date', 'running', '2026-11-15T10:00:00')")
    conn.commit()
    store.due(T0)
    store.due(T0)
    assert conn.execute("SELECT status FROM timers").fetchone()["status"] == "invalid"
    assert len([r for r in caplog.records if "unusable" in r.getMessage()]) == 1


def test_list_live_orders_a_paused_timer_by_its_frozen_remainder(store):
    paused = store.add(600, now=T0)           # 8 min left once paused
    store.pause(paused, T0 + timedelta(minutes=2))
    running = store.add(300, now=T0)           # 5 min left at T0+2m -> 3 min
    later = store.add(1200, now=T0)
    now = T0 + timedelta(minutes=2)
    assert [r["id"] for r in store.list_live(now)] == [running, paused, later]


def test_bump_ring_increments_and_returns_count(store):
    tid = store.add(60, now=T0)
    store.mark_ringing(tid, T0)
    assert store.bump_ring(tid) == 1
    assert store.bump_ring(tid) == 2


def test_snooze_rejected_unless_ringing(store):
    tid = store.add(60, now=T0)
    assert store.snooze(tid, 60, T0) is False


def test_bump_ring_leaves_a_dismissed_timer_unchanged(store):
    tid = store.add(60, now=T0)
    store.mark_ringing(tid, T0)
    store.bump_ring(tid)
    store.dismiss(tid)
    assert store.bump_ring(tid) == 1
    assert store.get(tid)["ring_count"] == 1


def test_mark_ringing_resets_ring_count_so_a_snooze_gets_a_fresh_budget(store):
    tid = store.add(60, now=T0)
    store.mark_ringing(tid, T0)
    store.bump_ring(tid)
    store.bump_ring(tid)
    store.snooze(tid, 60, T0)
    store.mark_ringing(tid, T0 + timedelta(minutes=1))
    assert store.get(tid)["ring_count"] == 0


def test_an_aware_now_does_not_retire_healthy_timers(store):
    from datetime import timezone
    tid = store.add(600, now=T0)
    aware = datetime(2026, 11, 15, 10, 1, 0).astimezone()
    assert aware.tzinfo is not None
    assert [r["id"] for r in store.list_live(aware)] == [tid]
    assert store.get(tid)["status"] == "running"


def test_recently_fired_still_retires_an_unparseable_fired_ts_outside_the_window(store, conn):
    good = store.add(60, "ok", now=T0)
    store.mark_ringing(good, T0)
    old = store.add(60, "old", now=T0)
    store.mark_ringing(old, T0 - timedelta(days=3))
    bad = store.add(60, "bad", now=T0)
    store.mark_ringing(bad, T0)
    conn.execute("UPDATE timers SET fired_ts = '0000-garbage' WHERE id = ?", (bad,))
    conn.commit()
    got = store.recently_fired(T0 + timedelta(minutes=5))
    assert [r["id"] for r in got] == [good]
    assert store.get(bad)["status"] == "invalid"
    assert store.get(old)["status"] == "ringing"  # out of window, left alone


def test_recently_fired_does_not_even_parse_rows_older_than_the_window(store, monkeypatch):
    # Proves the window is applied in SQL: an out-of-window row never reaches Python.
    from jarvisd import timer_store

    old = store.add(60, "old", now=T0)
    store.mark_ringing(old, T0 - timedelta(days=3))
    parsed = []
    real = timer_store.parse_ts
    monkeypatch.setattr(timer_store, "parse_ts", lambda v: (parsed.append(v), real(v))[1])
    assert store.recently_fired(T0 + timedelta(minutes=5)) == []
    assert parsed == []


# -- rename ----------------------------------------------------------------------


@pytest.mark.parametrize("prep", ["running", "paused", "ringing"])
def test_rename_changes_the_label_of_a_live_timer(store, prep):
    tid = store.add(600, "tea", now=T0)
    if prep == "paused":
        store.pause(tid, T0)
    elif prep == "ringing":
        store.mark_ringing(tid, T0)
    assert store.rename(tid, "chai") is True
    row = store.get(tid)
    assert row["label"] == "chai"
    assert row["status"] == prep


@pytest.mark.parametrize("finish", ["dismiss", "cancel"])
def test_rename_is_refused_for_a_finished_timer_and_leaves_the_label(store, finish):
    tid = store.add(600, "tea", now=T0)
    getattr(store, finish)(tid)
    assert store.rename(tid, "chai") is False
    assert store.get(tid)["label"] == "tea"


def test_rename_touches_only_the_named_timer_and_unknown_ids_return_false(store):
    a = store.add(600, "tea", now=T0)
    b = store.add(600, "pasta", now=T0)
    assert store.rename(a, "chai") is True
    assert store.get(b)["label"] == "pasta"
    assert store.rename(999, "x") is False
