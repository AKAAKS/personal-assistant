"""set_timer and timer_status tools, backed by TimerStore."""

import re
from datetime import datetime, timedelta

import pytest

from jarvisd import db
from jarvisd.reminder_store import ReminderStore
from jarvisd.timer_store import TimerStore
from jarvisd.tools.reminders import TOOLS, handlers

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
def reminders(conn):
    return ReminderStore(conn)


@pytest.fixture
def h(reminders, store):
    return handlers(reminders, store, clock=lambda: NOW)


def test_set_timer_reports_its_id_so_it_can_be_controlled(h, store):
    out = h["set_timer"]({"minutes": 20})
    assert f"#{store.list_live(NOW)[0]['id']}" in out


def test_set_timer_writes_to_the_timer_store_not_the_reminder_store(h, store, reminders):
    h["set_timer"]({"minutes": 10})
    assert len(store.list_live(NOW)) == 1
    assert reminders.list_active() == []


def test_set_timer_due_time_is_a_naive_local_timestamp_ten_minutes_out(h, store):
    h["set_timer"]({"minutes": 10})
    assert store.list_live(NOW)[0]["due_ts"] == "2026-11-15T10:10:00"


def test_set_timer_stores_the_label(h, store):
    h["set_timer"]({"minutes": 5, "label": " tea "})
    assert store.list_live(NOW)[0]["label"] == "tea"


def test_set_timer_confirmation_speaks_the_duration(h):
    assert "1 minute 30 seconds" in h["set_timer"]({"minutes": 1, "seconds": 30})


def test_set_timer_accepts_seconds(h, store):
    h["set_timer"]({"minutes": 1, "seconds": 30})
    assert store.list_live(NOW)[0]["duration_sec"] == 90


def test_set_timer_accepts_seconds_only_with_zero_minutes(h, store):
    h["set_timer"]({"minutes": 0, "seconds": 45})
    assert store.list_live(NOW)[0]["duration_sec"] == 45


@pytest.mark.parametrize("bad", [
    {"minutes": 0},
    {"minutes": -3},
    {"minutes": True},
    {"minutes": "ten"},
    {"minutes": 5, "seconds": -1},
    {"minutes": 5, "seconds": True},
])
def test_set_timer_rejects_bad_durations_and_creates_nothing(h, store, bad):
    assert "error" in h["set_timer"](bad).lower()
    assert store.list_live(NOW) == []


def test_set_timer_without_a_timer_store_says_so_instead_of_raising(reminders):
    out = handlers(reminders)["set_timer"]({"minutes": 5})
    assert "error" in out.lower()


def test_timer_status_speaks_remaining_time_not_a_timestamp(h):
    # The user asked "do I have a timer on right now?" and got nothing useful.
    h["set_timer"]({"minutes": 20, "label": "pasta"})
    out = h["timer_status"]({})
    assert "pasta" in out
    assert "20 minutes left" in out
    assert not re.search(r"\d{4}-\d{2}-\d{2}", out)
    assert not re.search(r"\d:\d{2}", out)


def test_timer_status_reports_the_remaining_time_as_of_now(store):
    store.add(600, "tea", now=NOW)
    later = handlers(None, store, clock=lambda: NOW + timedelta(seconds=330))
    assert "4 minutes 30 seconds left" in later["timer_status"]({})


def test_timer_status_says_a_paused_timer_is_paused(h, store):
    tid = store.add(600, "tea", now=NOW)
    store.pause(tid, NOW)
    out = h["timer_status"]({})
    assert "paused" in out and "10 minutes" in out


def test_timer_status_reports_a_timer_that_already_went_off(h, store):
    tid = store.add(60, "tea", now=NOW)
    store.mark_ringing(tid, NOW)
    out = h["timer_status"]({})
    assert "tea" in out and "ringing" in out


def test_timer_status_reports_a_dismissed_timer_that_went_off_recently(h, store):
    tid = store.add(60, "tea", now=NOW - timedelta(minutes=10))
    store.mark_ringing(tid, NOW - timedelta(minutes=9))
    store.dismiss(tid)
    out = h["timer_status"]({})
    assert "tea" in out and "9 minutes ago" in out and "dismissed" in out


def test_timer_status_mentions_a_ringing_timer_once(h, store):
    tid = store.add(60, "tea", now=NOW)
    store.mark_ringing(tid, NOW)
    assert h["timer_status"]({}).lower().count("tea") == 1


def test_timer_status_ignores_a_timer_that_went_off_long_ago(h, store):
    tid = store.add(60, "tea", now=NOW - timedelta(hours=3))
    store.mark_ringing(tid, NOW - timedelta(hours=3))
    store.dismiss(tid)
    assert "tea" not in h["timer_status"]({})


def test_timer_status_says_so_when_there_are_none(h):
    assert "no timers" in h["timer_status"]({}).lower()


def test_timer_status_lists_several_timers_soonest_first(h):
    h["set_timer"]({"minutes": 20, "label": "pasta"})
    h["set_timer"]({"minutes": 5, "label": "eggs"})
    out = h["timer_status"]({})
    assert "eggs" in out and "pasta" in out
    assert out.index("eggs") < out.index("pasta")


def test_timer_status_description_tells_the_model_to_call_it_for_is_it_done():
    desc = next(t for t in TOOLS if t["name"] == "timer_status")["description"].lower()
    assert "is my timer done" in desc and "how long is left" in desc


def test_set_timer_treats_null_seconds_and_null_label_as_absent(h, store):
    out = h["set_timer"]({"minutes": 20, "seconds": None, "label": None})
    assert "error" not in out.lower()
    row = store.list_live(NOW)[0]
    assert row["duration_sec"] == 1200 and row["label"] == ""


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_set_timer_rejects_non_finite_durations_with_an_error_not_an_exception(h, store, bad):
    assert "error" in h["set_timer"]({"minutes": bad}).lower()
    assert "error" in h["set_timer"]({"minutes": 1, "seconds": bad}).lower()
    assert store.list_live(NOW) == []


def test_timer_status_without_a_timer_store_says_so_instead_of_raising(reminders):
    assert "error" in handlers(reminders)["timer_status"]({}).lower()


def test_timer_control_description_says_it_takes_a_label():
    tool = next(t for t in TOOLS if t["name"] == "timer_control")
    assert "label" in tool["description"].lower()
    assert "label" in tool["input_schema"]["properties"]
