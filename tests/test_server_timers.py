"""The server rings a due timer as an alarm, again every RING_INTERVAL_SEC,
until the user dismisses it or MAX_RINGS is reached.

The bug this pins: a timer fired on schedule, was spoken once for 2.3 seconds,
and went quiet. The user missed it and reported "it didn't alert me".
"""

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from jarvisd import db
from jarvisd.server import MAX_RINGS, JarvisServer
from jarvisd.timer_store import TimerStore

pytestmark = pytest.mark.anyio  # no pytest-asyncio here; anyio ships its own plugin

T0 = datetime(2026, 11, 15, 10, 0, 0)


class FakeAssistant:
    conversation: list = []


class SpyNotifier:
    def __init__(self):
        self.calls = []
        self.fail_for = None

    def notify(self, *args, **kwargs):
        if self.fail_for and self.fail_for in args[0]:
            raise RuntimeError("channel down")
        self.calls.append(SimpleNamespace(args=args, kwargs=kwargs))


@pytest.fixture
def conn(tmp_path):
    return db.connect(str(tmp_path / "jarvis.db"))


@pytest.fixture
def timer_store(conn):
    return TimerStore(conn)


@pytest.fixture
def spy_notifier():
    return SpyNotifier()


@pytest.fixture
def server(timer_store, spy_notifier):
    return JarvisServer(FakeAssistant(), notifier=spy_notifier, timer_store=timer_store)


async def test_a_due_timer_rings_as_an_alarm(server, timer_store, spy_notifier):
    timer_store.add(60, "tea", now=T0)
    await server._fire_due_timers(now=T0 + timedelta(minutes=2))
    call = spy_notifier.calls[0]
    assert call.kwargs["alarm"] is True
    assert call.kwargs.get("priority", call.args[1]) == "urgent"
    assert "tea" in call.args[0]


async def test_a_ringing_timer_keeps_ringing_until_dismissed(server, timer_store, spy_notifier):
    # THE BUG: it alerted once, 2.3 seconds, and went quiet.
    tid = timer_store.add(60, now=T0)
    t = T0 + timedelta(minutes=2)
    await server._fire_due_timers(now=t)
    await server._fire_due_timers(now=t + timedelta(seconds=30))
    await server._fire_due_timers(now=t + timedelta(seconds=60))
    assert len(spy_notifier.calls) == 3
    timer_store.dismiss(tid)
    await server._fire_due_timers(now=t + timedelta(seconds=90))
    assert len(spy_notifier.calls) == 3


async def test_it_does_not_re_ring_before_the_interval(server, timer_store, spy_notifier):
    timer_store.add(60, now=T0)
    t = T0 + timedelta(minutes=2)
    await server._fire_due_timers(now=t)
    await server._fire_due_timers(now=t + timedelta(seconds=5))
    assert len(spy_notifier.calls) == 1


async def test_ringing_stops_and_auto_dismisses_at_max_rings(server, timer_store, spy_notifier):
    tid = timer_store.add(60, now=T0)
    t = T0 + timedelta(minutes=2)
    for i in range(30):
        await server._fire_due_timers(now=t + timedelta(seconds=30 * i))
    assert len(spy_notifier.calls) <= 20
    assert timer_store.get(tid)["status"] == "done"


async def test_a_paused_timer_never_rings(server, timer_store, spy_notifier):
    tid = timer_store.add(60, now=T0)
    timer_store.pause(tid, T0)
    await server._fire_due_timers(now=T0 + timedelta(hours=1))
    assert spy_notifier.calls == []


async def test_one_bad_timer_does_not_stop_the_others(server, timer_store, spy_notifier, conn):
    conn.execute("INSERT INTO timers (duration_sec, due_ts, status, created_ts) "
                 "VALUES (60, 'not-a-date', 'running', '2026-11-15T10:00:00')")
    conn.commit()
    timer_store.add(60, "good", now=T0)
    await server._fire_due_timers(now=T0 + timedelta(minutes=2))
    assert any("good" in c.args[0] for c in spy_notifier.calls)


async def test_a_timer_without_a_label_says_just_timers_up(server, timer_store, spy_notifier):
    timer_store.add(60, now=T0)
    await server._fire_due_timers(now=T0 + timedelta(minutes=2))
    assert spy_notifier.calls[0].args[0] == "Timer's up."


async def test_a_labelled_timer_names_itself_in_the_alarm(server, timer_store, spy_notifier):
    timer_store.add(60, "tea", now=T0)
    await server._fire_due_timers(now=T0 + timedelta(minutes=2))
    assert spy_notifier.calls[0].args[0] == "Timer's up — tea."


async def test_it_rings_exactly_max_rings_times_then_stops(server, timer_store, spy_notifier):
    tid = timer_store.add(60, now=T0)
    t = T0 + timedelta(minutes=2)
    for i in range(MAX_RINGS + 10):
        await server._fire_due_timers(now=t + timedelta(seconds=30 * i))
    assert len(spy_notifier.calls) == MAX_RINGS
    assert timer_store.get(tid)["status"] == "done"


async def test_a_snoozed_timer_gets_a_fresh_ring_budget(server, timer_store, spy_notifier):
    tid = timer_store.add(60, now=T0)
    t = T0 + timedelta(minutes=2)
    for i in range(5):
        await server._fire_due_timers(now=t + timedelta(seconds=30 * i))
    assert timer_store.get(tid)["ring_count"] == 5
    timer_store.snooze(tid, 60, t + timedelta(seconds=150))
    spy_notifier.calls.clear()
    t2 = t + timedelta(seconds=150 + 61)
    await server._fire_due_timers(now=t2)
    assert len(spy_notifier.calls) == 1
    assert timer_store.get(tid)["ring_count"] == 1
    await server._fire_due_timers(now=t2 + timedelta(seconds=30))
    assert len(spy_notifier.calls) == 2


async def test_a_failed_notify_is_retried_next_tick_and_spares_other_timers(
        server, timer_store, spy_notifier):
    timer_store.add(60, "bad", now=T0)
    timer_store.add(60, "good", now=T0)
    spy_notifier.fail_for = "bad"
    t = T0 + timedelta(minutes=2)
    await server._fire_due_timers(now=t)
    assert [c.args[0] for c in spy_notifier.calls] == ["Timer's up — good."]
    spy_notifier.fail_for = None
    await server._fire_due_timers(now=t + timedelta(seconds=5))
    assert any("bad" in c.args[0] for c in spy_notifier.calls)
