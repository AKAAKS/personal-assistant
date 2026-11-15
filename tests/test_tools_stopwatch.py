import re
from datetime import datetime, timedelta

import pytest

from jarvisd import db
from jarvisd.stopwatch_store import StopwatchStore
from jarvisd.tools import stopwatch as sw_tools

T0 = datetime(2026, 11, 15, 10, 0, 0)


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, **kw):
        self.now += timedelta(**kw)


@pytest.fixture
def conn(tmp_path):
    c = db.connect(str(tmp_path / "t.db"))
    yield c
    c.close()


@pytest.fixture
def store(conn):
    return StopwatchStore(conn)


@pytest.fixture
def clock():
    return Clock(T0)


@pytest.fixture
def h(store, clock):
    return sw_tools.handlers(store, clock=clock)


def test_tool_is_registered_with_a_distinguishing_description():
    (tool,) = sw_tools.TOOLS
    assert tool["name"] == "stopwatch_control"
    desc = tool["description"].lower()
    assert "counts up" in desc
    assert "not a countdown" in desc
    assert "set_timer" in tool["description"]
    assert tool["input_schema"]["properties"]["action"]["enum"] == [
        "start", "pause", "resume", "stop", "check"]
    assert tool["input_schema"]["required"] == ["action"]


def test_check_speaks_the_elapsed_time(h, store, clock):
    store.start("run", now=T0)
    clock.advance(minutes=4, seconds=30)
    out = h["stopwatch_control"]({"action": "check"})
    assert "run" in out
    assert "4 minutes 30 seconds" in out
    assert not re.search(r"\d:\d\d", out)


def test_check_with_no_stopwatch_does_not_raise(h):
    assert "no" in h["stopwatch_control"]({"action": "check"}).lower()


def test_start_creates_a_labelled_running_stopwatch(h, store):
    out = h["stopwatch_control"]({"action": "start", "label": "laps"})
    (row,) = store.active(T0)
    assert row["label"] == "laps" and row["status"] == "running"
    assert "laps" in out


@pytest.mark.parametrize("ti", [
    {"action": "start", "label": None},
    {"action": "start"},
    {"action": "start", "label": "   "},
])
def test_start_tolerates_a_null_missing_or_blank_label(h, store, ti):
    h["stopwatch_control"](ti)
    (row,) = store.active(T0)
    assert row["label"] == ""


def test_check_and_stop_tolerate_an_explicit_null_label(h, store, clock):
    store.start(now=T0)
    clock.advance(seconds=10)
    assert "10 seconds" in h["stopwatch_control"]({"action": "check", "label": None})
    assert "10 seconds" in h["stopwatch_control"]({"action": "stop", "label": None})


def test_pause_then_check_reports_frozen_time_and_says_paused(h, store, clock):
    store.start(now=T0)
    clock.advance(seconds=30)
    h["stopwatch_control"]({"action": "pause"})
    clock.advance(hours=3)
    out = h["stopwatch_control"]({"action": "check"})
    assert "30 seconds" in out
    assert "paused" in out.lower()


def test_resume_continues_from_the_frozen_time(h, store, clock):
    store.start(now=T0)
    clock.advance(seconds=30)
    h["stopwatch_control"]({"action": "pause"})
    clock.advance(minutes=10)
    h["stopwatch_control"]({"action": "resume"})
    clock.advance(seconds=15)
    assert "45 seconds" in h["stopwatch_control"]({"action": "check"})


def test_stop_speaks_the_total_and_retires_the_stopwatch(h, store, clock):
    store.start("run", now=T0)
    clock.advance(seconds=90)
    out = h["stopwatch_control"]({"action": "stop"})
    assert "1 minute 30 seconds" in out
    assert store.active(clock()) == []


def test_pause_on_an_already_paused_stopwatch_says_so(h, store, clock):
    store.start(now=T0)
    h["stopwatch_control"]({"action": "pause"})
    assert "already paused" in h["stopwatch_control"]({"action": "pause"}).lower()


def test_resume_on_a_running_stopwatch_says_it_is_already_running(h, store):
    store.start(now=T0)
    assert "already running" in h["stopwatch_control"]({"action": "resume"}).lower()


@pytest.mark.parametrize("action", ["pause", "resume", "stop"])
def test_control_actions_with_no_stopwatch_do_not_raise(h, action):
    assert "no" in h["stopwatch_control"]({"action": action}).lower()


def test_unknown_or_missing_action_is_an_error_string(h):
    assert h["stopwatch_control"]({"action": "rewind"}).startswith("Error")
    assert h["stopwatch_control"]({}).startswith("Error")
    assert h["stopwatch_control"]({"action": None}).startswith("Error")


def test_several_stopwatches_need_a_label_to_disambiguate(h, store, clock):
    store.start("a", now=T0)
    store.start("b", now=T0)
    clock.advance(seconds=20)
    out = h["stopwatch_control"]({"action": "stop"})
    assert "which" in out.lower()
    assert len(store.active(clock())) == 2


def test_a_label_selects_one_of_several_case_insensitively(h, store, clock):
    store.start("Alpha", now=T0)
    store.start("Beta", now=T0)
    clock.advance(seconds=20)
    h["stopwatch_control"]({"action": "stop", "label": "beta"})
    assert [r["label"] for r in store.active(clock())] == ["Alpha"]


def test_unknown_label_reports_no_match_and_leaves_state_alone(h, store, clock):
    store.start("a", now=T0)
    out = h["stopwatch_control"]({"action": "stop", "label": "zzz"})
    assert "zzz" in out
    assert len(store.active(clock())) == 1


def test_check_with_several_lists_each_with_its_time(h, store, clock):
    store.start("a", now=T0)
    store.start("b", now=T0)
    clock.advance(minutes=2)
    out = h["stopwatch_control"]({"action": "check"})
    assert "The a stopwatch is at 2 minutes." in out
    assert "The b stopwatch is at 2 minutes." in out


# ---- duplicate / unaddressable guard ------------------------------------

@pytest.mark.parametrize("label", [None, "run", "RUN"])
def test_two_starts_then_a_bare_stop_cannot_reach_an_unaddressable_state(h, store, clock, label):
    first = {"action": "start", "label": "run" if label else None}
    h["stopwatch_control"](first)
    clock.advance(seconds=5)
    again = h["stopwatch_control"]({"action": "start", "label": label})
    assert "already running" in again
    assert len(store.active(clock())) == 1
    clock.advance(seconds=5)
    out = h["stopwatch_control"]({"action": "stop", "label": label})
    assert "10 seconds" in out
    assert store.active(clock()) == []


def test_start_on_a_paused_label_reports_paused_and_makes_no_duplicate(h, store, clock):
    h["stopwatch_control"]({"action": "start", "label": "run"})
    clock.advance(seconds=30)
    h["stopwatch_control"]({"action": "pause", "label": "run"})
    out = h["stopwatch_control"]({"action": "start", "label": "run"})
    assert "already paused" in out
    assert len(store.active(clock())) == 1


def test_different_labels_still_start_separately(h, store, clock):
    h["stopwatch_control"]({"action": "start", "label": "a"})
    h["stopwatch_control"]({"action": "start", "label": "b"})
    h["stopwatch_control"]({"action": "start"})
    assert len(store.active(clock())) == 3


# ---- spoken wording -----------------------------------------------------

def test_a_label_keeps_its_case_in_the_resume_reply(h, store, clock):
    h["stopwatch_control"]({"action": "start", "label": "NYC"})
    h["stopwatch_control"]({"action": "pause"})
    assert "NYC" in h["stopwatch_control"]({"action": "resume"})


def test_zero_elapsed_never_speaks_no_time(h):
    h["stopwatch_control"]({"action": "start"})
    out = h["stopwatch_control"]({"action": "check"})
    assert "no time" not in out
    assert "hasn't counted" in out
    stopped = h["stopwatch_control"]({"action": "stop"})
    assert "no time" not in stopped
    assert "less than a second" in stopped


def test_pause_reply_starts_with_paused_and_speaks_the_time(h, store, clock):
    store.start(now=T0)
    clock.advance(seconds=30)
    out = h["stopwatch_control"]({"action": "pause"})
    assert out.startswith("Paused.")
    assert "30 seconds" in out


# ---- failure branches ---------------------------------------------------

class RefusingStore(StopwatchStore):
    """A store whose mutation is lost to a race after the handler picked the row."""

    def pause(self, sw_id, now):
        return False

    def resume(self, sw_id, now):
        return False

    def stop(self, sw_id, now):
        return None


@pytest.mark.parametrize("action,expected", [
    ("pause", "could not be paused"),
    ("resume", "could not be resumed"),
    ("stop", "could not be stopped"),
])
def test_a_lost_mutation_is_reported_not_raised(conn, clock, action, expected):
    rs = RefusingStore(conn)
    sid = rs.start(now=T0)
    if action == "resume":
        conn.execute("UPDATE stopwatches SET status = 'paused' WHERE id = ?", (sid,))
        conn.commit()
    out = sw_tools.handlers(rs, clock=clock)["stopwatch_control"]({"action": action})
    assert expected in out


def test_a_row_with_a_bad_started_ts_is_skipped_by_the_tool(h, conn):
    conn.execute(
        "INSERT INTO stopwatches (label, started_ts, status) VALUES ('bad', 'garbage', 'running')")
    conn.commit()
    assert "no stopwatch" in h["stopwatch_control"]({"action": "stop"}).lower()
