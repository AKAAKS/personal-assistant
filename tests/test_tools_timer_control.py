"""timer_control: resolving which timer, and each action."""

from datetime import datetime

import pytest

from jarvisd import db
from jarvisd.timer_store import TimerStore
from jarvisd.tools.reminders import handlers

NOW = datetime(2026, 11, 15, 10, 0, 0)


@pytest.fixture
def conn(tmp_path):
    c = db.connect(str(tmp_path / "t.db"))
    yield c
    c.close()


@pytest.fixture
def store(conn):
    return TimerStore(conn)


@pytest.fixture
def h(store):
    return handlers(None, store, clock=lambda: NOW)


def test_pause_and_resume_by_label(h, store):
    h["set_timer"]({"minutes": 10, "label": "tea"})
    assert "paused" in h["timer_control"]({"action": "pause", "label": "tea"}).lower()
    assert store.list_live(NOW)[0]["status"] == "paused"
    assert "resumed" in h["timer_control"]({"action": "resume", "label": "tea"}).lower()
    assert store.list_live(NOW)[0]["status"] == "running"


def test_label_matching_is_case_insensitive(h, store):
    h["set_timer"]({"minutes": 10, "label": "Tea"})
    h["timer_control"]({"action": "pause", "label": "tEA"})
    assert store.list_live(NOW)[0]["status"] == "paused"


def test_control_by_timer_id_beats_a_label_that_matches_nothing(h, store):
    tid = store.add(600, "tea", now=NOW)
    h["timer_control"]({"action": "pause", "timer_id": tid, "label": "ghost"})
    assert store.get(tid)["status"] == "paused"


def test_extend_adds_minutes_and_says_the_new_time(h, store):
    h["set_timer"]({"minutes": 10, "label": "tea"})
    out = h["timer_control"]({"action": "extend", "label": "tea", "minutes": 5})
    assert "15 minutes left" in out
    assert store.list_live(NOW)[0]["due_ts"] == "2026-11-15T10:15:00"


def test_cancel_removes_the_timer(h, store):
    h["set_timer"]({"minutes": 10, "label": "tea"})
    assert "cancelled" in h["timer_control"]({"action": "cancel", "label": "tea"}).lower()
    assert store.list_live(NOW) == []


def test_dismiss_stops_a_ringing_timer(h, store):
    tid = store.add(60, "tea", now=NOW)
    store.mark_ringing(tid, NOW)
    assert "dismissed" in h["timer_control"]({"action": "dismiss", "label": "tea"}).lower()
    assert store.get(tid)["status"] == "done"


def test_snooze_a_ringing_timer_for_the_given_minutes(h, store):
    tid = store.add(60, "tea", now=NOW)
    store.mark_ringing(tid, NOW)
    out = h["timer_control"]({"action": "snooze", "label": "tea", "minutes": 9})
    assert "9 minutes" in out
    row = store.get(tid)
    assert row["status"] == "running" and row["due_ts"] == "2026-11-15T10:09:00"


def test_snooze_defaults_to_five_minutes(h, store):
    tid = store.add(60, "tea", now=NOW)
    store.mark_ringing(tid, NOW)
    h["timer_control"]({"action": "snooze", "label": "tea"})
    assert store.get(tid)["due_ts"] == "2026-11-15T10:05:00"


def test_snoozing_a_timer_that_is_not_ringing_reports_it_did_not_apply(h, store):
    tid = store.add(600, "tea", now=NOW)
    out = h["timer_control"]({"action": "snooze", "label": "tea", "minutes": 5})
    assert "couldn't snooze" in out.lower()
    assert store.get(tid)["due_ts"] == "2026-11-15T10:10:00"


def test_control_targets_the_only_timer_when_nothing_is_named(h, store):
    h["set_timer"]({"minutes": 10})
    assert "paused" in h["timer_control"]({"action": "pause"}).lower()
    assert store.list_live(NOW)[0]["status"] == "paused"


def test_control_asks_which_one_when_several_are_live_and_changes_nothing(h, store):
    h["set_timer"]({"minutes": 10, "label": "tea"})
    h["set_timer"]({"minutes": 20, "label": "pasta"})
    out = h["timer_control"]({"action": "pause"})
    assert "which timer" in out.lower()
    assert "tea" in out and "pasta" in out
    assert {r["status"] for r in store.list_live(NOW)} == {"running"}


def test_control_asks_which_one_when_a_label_matches_several(h, store):
    store.add(600, "tea", now=NOW)
    store.add(900, "tea", now=NOW)
    out = h["timer_control"]({"action": "pause", "label": "tea"})
    assert "which timer" in out.lower()
    assert {r["status"] for r in store.list_live(NOW)} == {"running"}


def test_dismiss_with_no_target_picks_the_one_ringing_timer_beside_running_ones(h, store):
    ringing = store.add(60, "tea", now=NOW)
    store.mark_ringing(ringing, NOW)
    other = store.add(900, "pasta", now=NOW)
    h["timer_control"]({"action": "dismiss"})
    assert store.get(ringing)["status"] == "done"
    assert store.get(other)["status"] == "running"


@pytest.mark.parametrize("action", ["pause", "cancel"])
def test_a_stray_zero_minutes_is_ignored_by_actions_that_do_not_use_it(h, store, action):
    tid = store.add(600, "tea", now=NOW)
    out = h["timer_control"]({"action": action, "label": "tea", "minutes": 0})
    assert "error" not in out.lower()
    assert store.get(tid)["status"] == {"pause": "paused", "cancel": "cancelled"}[action]


def test_null_minutes_label_and_timer_id_are_treated_as_absent(h, store):
    tid = store.add(60, "tea", now=NOW)
    store.mark_ringing(tid, NOW)
    out = h["timer_control"]({"action": "snooze", "minutes": None, "label": None, "timer_id": None})
    assert "error" not in out.lower()
    assert store.get(tid)["due_ts"] == "2026-11-15T10:05:00"


def test_extend_rejects_non_finite_minutes(h, store):
    store.add(600, "tea", now=NOW)
    assert "error" in h["timer_control"]({"action": "extend", "minutes": float("nan")}).lower()


def test_dismiss_with_no_target_and_two_ringing_timers_asks_and_changes_nothing(h, store):
    a = store.add(60, "tea", now=NOW)
    b = store.add(60, "eggs", now=NOW)
    store.mark_ringing(a, NOW)
    store.mark_ringing(b, NOW)
    out = h["timer_control"]({"action": "dismiss"})
    assert "which timer" in out.lower() and "tea" in out and "eggs" in out
    assert store.get(a)["status"] == "ringing" and store.get(b)["status"] == "ringing"


def test_dismiss_with_no_target_ends_a_lone_running_timer(h, store):
    tid = store.add(600, "tea", now=NOW)
    assert "dismissed" in h["timer_control"]({"action": "dismiss"}).lower()
    assert store.get(tid)["status"] == "done"


def test_control_on_an_unknown_label_does_not_raise(h):
    assert "no live timer" in h["timer_control"]({"action": "pause", "label": "ghost"}).lower()


def test_control_on_an_unknown_id_does_not_raise(h):
    assert "no live timer #99" in h["timer_control"]({"action": "pause", "timer_id": 99}).lower()


def test_control_with_no_timers_at_all_says_so(h):
    assert "no timers" in h["timer_control"]({"action": "pause"}).lower()


def test_a_finished_timer_cannot_be_controlled_by_id(h, store):
    tid = store.add(60, "tea", now=NOW)
    store.mark_ringing(tid, NOW)
    store.dismiss(tid)
    assert "no live timer" in h["timer_control"]({"action": "pause", "timer_id": tid}).lower()


def test_pausing_an_already_paused_timer_reports_it_did_not_apply(h, store):
    tid = store.add(600, "tea", now=NOW)
    store.pause(tid, NOW)
    assert "couldn't pause" in h["timer_control"]({"action": "pause", "label": "tea"}).lower()


@pytest.mark.parametrize("minutes", [None, 0, -2, True])
def test_extend_without_valid_minutes_is_an_error_and_changes_nothing(h, store, minutes):
    h["set_timer"]({"minutes": 10, "label": "tea"})
    args = {"action": "extend", "label": "tea"}
    if minutes is not None:
        args["minutes"] = minutes
    assert "error" in h["timer_control"](args).lower()
    assert store.list_live(NOW)[0]["due_ts"] == "2026-11-15T10:10:00"


def test_an_unknown_action_is_an_error(h):
    assert "error" in h["timer_control"]({"action": "obliterate"}).lower()


def test_a_non_integer_timer_id_is_an_error(h):
    assert "error" in h["timer_control"]({"action": "pause", "timer_id": "3"}).lower()


def test_control_without_a_timer_store_says_so_instead_of_raising():
    out = handlers(None)["timer_control"]({"action": "pause"})
    assert "error" in out.lower()


@pytest.mark.parametrize("action", ["extend", "snooze"])
@pytest.mark.parametrize("minutes", [1e30, 1e308])
def test_an_absurdly_large_duration_is_a_clean_error_not_an_exception(h, store, action, minutes):
    h["set_timer"]({"minutes": 10, "label": "tea"})
    tid = store.list_live(NOW)[0]["id"]
    if action == "snooze":
        store.mark_ringing(tid, NOW)
    result = h["timer_control"]({"action": action, "label": "tea", "minutes": minutes})
    assert result.startswith("Error:") and "minutes" in result
    assert store.get(tid)["status"] == ("ringing" if action == "snooze" else "running")


def test_extending_a_paused_timer_by_an_absurd_duration_is_a_clean_error(h, store):
    h["set_timer"]({"minutes": 10, "label": "tea"})
    h["timer_control"]({"action": "pause", "label": "tea"})
    result = h["timer_control"]({"action": "extend", "label": "tea", "minutes": 1e30})
    assert result.startswith("Error:") and "minutes" in result
