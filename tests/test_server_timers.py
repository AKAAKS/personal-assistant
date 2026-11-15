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


async def test_a_failed_notify_spares_other_timers_and_the_next_ring_is_on_schedule(
        server, timer_store, spy_notifier):
    timer_store.add(60, "bad", now=T0)
    timer_store.add(60, "good", now=T0)
    spy_notifier.fail_for = "bad"
    t = T0 + timedelta(minutes=2)
    await server._fire_due_timers(now=t)
    assert [c.args[0] for c in spy_notifier.calls] == ["Timer's up — good."]
    spy_notifier.fail_for = None
    # The failed attempt used up its slot: no hot 5-second retry loop...
    await server._fire_due_timers(now=t + timedelta(seconds=5))
    assert not any("bad" in c.args[0] for c in spy_notifier.calls)
    # ...the next attempt is on the normal 30-second schedule.
    await server._fire_due_timers(now=t + timedelta(seconds=30))
    assert any("bad" in c.args[0] for c in spy_notifier.calls)


@pytest.mark.parametrize("bad_fired", [None, "not-a-date"])
async def test_a_ringing_row_with_a_bad_fired_ts_does_not_silence_the_others(
        server, timer_store, spy_notifier, conn, bad_fired):
    # Inserted ringing (not running): list_live would retire a bad running row
    # before the ring loop is ever reached, so only this shape tests the loop.
    conn.execute("INSERT INTO timers (label, duration_sec, due_ts, status, created_ts, fired_ts) "
                 "VALUES ('bad', 60, '2026-11-15T10:01:00', 'ringing', '2026-11-15T10:00:00', ?)",
                 (bad_fired,))
    conn.commit()
    good = timer_store.add(60, "good", now=T0)
    t = T0 + timedelta(minutes=2)
    timer_store.mark_ringing(good, t)
    await server._fire_due_timers(now=t + timedelta(seconds=1))
    assert any("good" in c.args[0] for c in spy_notifier.calls) or \
        timer_store.get(good)["ring_count"] == 0
    # The good timer is not yet due to re-ring (count 0 -> due at fired+0): it rings.
    assert any("good" in c.args[0] for c in spy_notifier.calls)


async def test_a_timer_dismissed_mid_tick_is_not_rung(server, timer_store, spy_notifier):
    tid = timer_store.add(60, "tea", now=T0)
    t = T0 + timedelta(minutes=2)
    timer_store.mark_ringing(tid, t)
    ringing_row = timer_store.get(tid)
    timer_store.dismiss(tid)
    await server._ring(ringing_row)
    assert spy_notifier.calls == []


async def test_a_persistently_failing_notify_still_ends_at_max_rings(
        server, timer_store, spy_notifier):
    tid = timer_store.add(60, "bad", now=T0)
    spy_notifier.fail_for = "bad"
    t = T0 + timedelta(minutes=2)
    for i in range(MAX_RINGS + 5):
        await server._fire_due_timers(now=t + timedelta(seconds=30 * i))
    assert timer_store.get(tid)["status"] == "done"


async def test_a_user_turn_runs_the_assistant_off_the_event_loop_thread(timer_store, spy_notifier):
    """Timer and stopwatch handlers are synchronous sqlite calls made from inside
    Assistant.process. They are only safe because _process hands that to a worker
    thread; on the loop thread a slow query would freeze the whole daemon."""
    import threading

    seen = {}

    class ThreadSpyAssistant(FakeAssistant):
        def process(self, text):
            seen["thread"] = threading.get_ident()
            return "ok"

    server = JarvisServer(ThreadSpyAssistant(), notifier=spy_notifier, timer_store=timer_store)
    assert await server._process("set a timer") == "ok"
    assert seen["thread"] != threading.get_ident()


async def test_a_ringing_timer_stranded_by_an_outage_is_dismissed_not_rung(
        server, timer_store, spy_notifier):
    # Lid closed / daemon killed mid-ring: the row is still 'ringing' hours later.
    tid = timer_store.add(60, "old", now=T0)
    timer_store.mark_ringing(tid, T0)
    for _ in range(3):
        timer_store.bump_ring(tid)
    await server._fire_due_timers(now=T0 + timedelta(hours=1))
    assert spy_notifier.calls == []
    assert timer_store.get(tid)["status"] == "done"


async def test_a_ringing_timer_inside_the_ring_window_still_rings(
        server, timer_store, spy_notifier):
    # The counterpart to the outage test: the staleness bound must not be over-broad.
    tid = timer_store.add(60, "fresh", now=T0)
    timer_store.mark_ringing(tid, T0)
    timer_store.bump_ring(tid)  # 1 ring done; the 2nd lands at T0 + 30s
    await server._fire_due_timers(now=T0 + timedelta(seconds=30))
    assert [c.args[0] for c in spy_notifier.calls] == ["Timer's up — fresh."]
    assert timer_store.get(tid)["status"] == "ringing"
    assert timer_store.get(tid)["ring_count"] == 2


async def test_a_snooze_landing_during_the_last_ring_is_not_cancelled(
        server, timer_store, spy_notifier, monkeypatch):
    tid = timer_store.add(60, "tea", now=T0)
    t = T0 + timedelta(minutes=2)
    await server._fire_due_timers(now=t)
    real_bump = timer_store.bump_ring

    def bump_then_user_snoozes(timer_id):
        real_bump(timer_id)
        timer_store.snooze(timer_id, 300, t)  # the user's snooze lands in the gap
        return MAX_RINGS  # this was ring 20 of 20

    monkeypatch.setattr(timer_store, "bump_ring", bump_then_user_snoozes)
    await server._fire_due_timers(now=t + timedelta(seconds=30))
    assert timer_store.get(tid)["status"] == "running"


# --- ring pacing across a stalled tick loop, and one banked notice ------------

async def _drive(server, spy_notifier, start, seconds, step=5):
    """Tick every `step` seconds; return the offsets (from `start`) that rang."""
    rang = []
    for off in range(0, seconds + 1, step):
        before = len(spy_notifier.calls)
        await server._fire_due_timers(now=start + timedelta(seconds=off))
        if len(spy_notifier.calls) > before:
            rang.append(off)
    return rang


async def test_a_sub_window_stall_does_not_collapse_the_ring_to_the_tick_interval(
        server, timer_store, spy_notifier):
    timer_store.add(60, "tea", now=T0)
    fire = T0 + timedelta(minutes=2)
    await server._fire_due_timers(now=fire)          # ring 1 at offset 0
    resume = fire + timedelta(seconds=150)           # laptop slept 150s (< 600s window)
    rang = await _drive(server, spy_notifier, resume, 70)
    gaps = [b - a for a, b in zip(rang, rang[1:])]
    assert rang[0] == 0 and gaps and all(g == 30 for g in gaps)
    assert rang == [0, 30, 60]


async def test_an_unstalled_ring_keeps_its_30_second_cadence(server, timer_store, spy_notifier):
    timer_store.add(60, "tea", now=T0)
    fire = T0 + timedelta(minutes=2)
    rang = await _drive(server, spy_notifier, fire, 120)
    assert rang == [0, 30, 60, 90, 120]


async def test_a_stall_followed_by_a_fresh_daemon_rings_once_then_paces(
        server, timer_store, spy_notifier):
    timer_store.add(60, "tea", now=T0)
    fire = T0 + timedelta(minutes=2)
    await server._fire_due_timers(now=fire)
    # The daemon restarts: a new server, an empty pacing memory, the same DB.
    fresh = JarvisServer(FakeAssistant(), notifier=spy_notifier, timer_store=timer_store)
    spy_notifier.calls.clear()
    rang = await _drive(fresh, spy_notifier, fire + timedelta(seconds=150), 70)
    assert rang == [0, 30, 60]


async def test_the_pacing_memory_is_dropped_when_a_timer_stops_ringing(
        server, timer_store, spy_notifier):
    tid = timer_store.add(60, "tea", now=T0)
    fire = T0 + timedelta(minutes=2)
    await server._fire_due_timers(now=fire)
    await server._fire_due_timers(now=fire + timedelta(seconds=30))
    assert tid in server._last_ring
    timer_store.dismiss(tid)
    await server._fire_due_timers(now=fire + timedelta(seconds=35))
    assert tid not in server._last_ring


async def test_a_snoozed_timer_that_rings_again_is_not_held_back_by_its_old_ring(
        server, timer_store, spy_notifier):
    tid = timer_store.add(60, "tea", now=T0)
    fire = T0 + timedelta(minutes=2)
    await server._fire_due_timers(now=fire)
    timer_store.snooze(tid, 1, fire + timedelta(seconds=2))
    spy_notifier.calls.clear()
    await server._fire_due_timers(now=fire + timedelta(seconds=10))  # due again, 10s after ring
    assert len(spy_notifier.calls) == 1


def _real_notifier(conn, **kw):
    from jarvisd.notifier import Notifier
    return Notifier(conn, quiet_hours=None, now=lambda: T0, **kw)


def _queued(conn):
    return [r["text"] for r in conn.execute("SELECT text FROM notice_queue ORDER BY id")]


async def test_every_channel_failing_banks_exactly_one_notice_over_a_full_window(
        conn, timer_store):
    notifier = _real_notifier(conn, local_notify=lambda text: False)
    server = JarvisServer(FakeAssistant(), notifier=notifier, timer_store=timer_store)
    timer_store.add(60, "tea", now=T0)
    fire = T0 + timedelta(minutes=2)
    for i in range(MAX_RINGS + 3):
        await server._fire_due_timers(now=fire + timedelta(seconds=30 * i))
    assert _queued(conn) == ["Timer's up — tea."]


async def test_a_ring_that_reaches_a_channel_banks_nothing(conn, timer_store):
    notifier = _real_notifier(conn, local_notify=lambda text: True)
    server = JarvisServer(FakeAssistant(), notifier=notifier, timer_store=timer_store)
    timer_store.add(60, "tea", now=T0)
    fire = T0 + timedelta(minutes=2)
    for i in range(5):
        await server._fire_due_timers(now=fire + timedelta(seconds=30 * i))
    assert _queued(conn) == []


async def test_only_the_first_ring_may_be_banked(server, timer_store, spy_notifier):
    timer_store.add(60, "tea", now=T0)
    fire = T0 + timedelta(minutes=2)
    for i in range(3):
        await server._fire_due_timers(now=fire + timedelta(seconds=30 * i))
    flags = [c.kwargs.get("perishable", False) for c in spy_notifier.calls]
    assert flags == [False, True, True]
