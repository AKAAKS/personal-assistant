"""A perishable notice is delivered now or not at all -- never banked.

"Claude has been waiting on you" is only true for as long as it is true. The
notice queue is the opposite: `drain()` appends banked notices to the next turn,
so a notice queued at midnight is read out at 8am about a session that ended
eight hours earlier. That is the design's fourth anti-goal -- announcing
something that is no longer true -- and it is worse than silence.

So `perishable=True` skips the queue entirely: if no route delivers right now,
drop it and log. It is keyword-only, and the default must not change, because
reminders and timers DO want to be banked.
"""

from datetime import datetime

from tests.test_notifier_local import FakeHAClient, FakeLocal, fixed_now, make_notifier

QUIET = datetime(2026, 11, 12, 1, 30)   # inside the default 23:00-08:00 window
AWAKE = datetime(2026, 11, 12, 14, 0)


def _queued(conn):
    return [r["text"] for r in
            conn.execute("SELECT text FROM notice_queue ORDER BY id").fetchall()]


def test_a_perishable_notice_is_dropped_in_quiet_hours(tmp_path):
    notifier, conn = make_notifier(tmp_path, now=fixed_now(QUIET))
    notifier.push_to_clients = lambda text: 1
    notifier.notify("Claude needs your input in proj.", priority="high",
                    perishable=True)
    assert _queued(conn) == []


def test_a_perishable_notice_is_dropped_when_no_route_delivers(tmp_path):
    notifier, conn = make_notifier(tmp_path, now=fixed_now(AWAKE))
    notifier.push_to_clients = lambda text: 0
    notifier.notify("Claude needs your input in proj.", priority="high",
                    perishable=True)
    assert _queued(conn) == []


def test_a_perishable_notice_still_speaks_when_a_client_is_connected(tmp_path):
    spoken = []
    notifier, conn = make_notifier(tmp_path, now=fixed_now(AWAKE))
    notifier.push_to_clients = lambda text: spoken.append(text) or 1
    notifier.notify("Claude finished in proj.", priority="high", perishable=True)
    assert spoken == ["Claude finished in proj."]
    assert _queued(conn) == []


def test_a_perishable_notice_still_falls_back_to_the_local_toast(tmp_path):
    local = FakeLocal(result=True)
    notifier, conn = make_notifier(tmp_path, now=fixed_now(AWAKE),
                                   local_notify=local)
    notifier.push_to_clients = lambda text: 0
    notifier.notify("Claude finished in proj.", priority="high", perishable=True)
    assert local.calls == ["Claude finished in proj."]
    assert _queued(conn) == []


def test_a_perishable_notice_still_falls_back_to_the_phone_push(tmp_path):
    ha = FakeHAClient()
    notifier, conn = make_notifier(tmp_path, now=fixed_now(AWAKE), ha_client=ha)
    notifier.push_to_clients = lambda text: 0
    notifier.notify("Claude finished in proj.", priority="high", perishable=True)
    assert [c[0] for c in ha.calls] == ["Claude finished in proj."]
    assert _queued(conn) == []


def test_a_low_priority_perishable_notice_is_dropped_not_queued(tmp_path):
    """Low priority always queues today, which for a perishable notice is the
    bug this flag exists to prevent."""
    notifier, conn = make_notifier(tmp_path, now=fixed_now(AWAKE))
    notifier.notify("Claude finished in proj.", priority="low", perishable=True)
    assert _queued(conn) == []


def test_a_normal_notice_still_queues(tmp_path):
    """The default must not change: reminders keep banking."""
    notifier, conn = make_notifier(tmp_path, now=fixed_now(AWAKE))
    notifier.push_to_clients = lambda text: 0
    notifier.notify("Time to take the bins out.", priority="high")
    assert _queued(conn) == ["Time to take the bins out."]


def test_a_normal_notice_still_queues_in_quiet_hours(tmp_path):
    notifier, conn = make_notifier(tmp_path, now=fixed_now(QUIET))
    notifier.push_to_clients = lambda text: 1
    notifier.notify("Time to take the bins out.", priority="high")
    assert _queued(conn) == ["Time to take the bins out."]


def test_a_perishable_urgent_notice_ignores_quiet_hours_like_any_other(tmp_path):
    spoken = []
    notifier, conn = make_notifier(tmp_path, now=fixed_now(QUIET))
    notifier.push_to_clients = lambda text: spoken.append(text) or 1
    notifier.notify("Claude needs your input in proj.", priority="urgent",
                    perishable=True)
    assert spoken == ["Claude needs your input in proj."]
    assert _queued(conn) == []


def test_a_local_only_notice_does_not_reach_the_phone(tmp_path):
    """The Claude-session notices ignore quiet hours so they still reach
    someone working at 2am. Escalating those to a phone push means "Claude
    finished" buzzing a phone at 4am, which nobody asked for."""
    ha = FakeHAClient()
    notifier, conn = make_notifier(tmp_path, now=fixed_now(QUIET), ha_client=ha)
    notifier.push_to_clients = lambda text: 0
    notifier.notify("Claude needs your input in proj.", priority="urgent",
                    perishable=True, local_only=True)
    assert ha.calls == []
    assert _queued(conn) == []


def test_a_local_only_notice_still_uses_the_local_toast(tmp_path):
    local = FakeLocal(result=True)
    ha = FakeHAClient()
    notifier, conn = make_notifier(tmp_path, now=fixed_now(QUIET),
                                   ha_client=ha, local_notify=local)
    notifier.push_to_clients = lambda text: 0
    notifier.notify("Claude needs your input in proj.", priority="urgent",
                    perishable=True, local_only=True)
    assert local.calls == ["Claude needs your input in proj."]
    assert ha.calls == []


def test_a_local_only_notice_still_speaks_to_a_connected_client(tmp_path):
    spoken = []
    ha = FakeHAClient()
    notifier, conn = make_notifier(tmp_path, now=fixed_now(QUIET), ha_client=ha)
    notifier.push_to_clients = lambda text: spoken.append(text) or 1
    notifier.notify("Claude finished in proj.", priority="urgent",
                    perishable=True, local_only=True)
    assert spoken == ["Claude finished in proj."]
    assert ha.calls == []


def test_the_phone_push_is_untouched_for_a_normal_notice(tmp_path):
    """The default must not change: a reminder with no route still reaches the
    phone."""
    ha = FakeHAClient()
    notifier, conn = make_notifier(tmp_path, now=fixed_now(AWAKE), ha_client=ha)
    notifier.push_to_clients = lambda text: 0
    notifier.notify("Reminder: bins out.", priority="high")
    assert [c[0] for c in ha.calls] == ["Reminder: bins out."]


def test_local_only_is_keyword_only(tmp_path):
    import inspect

    from jarvisd.notifier import Notifier
    param = inspect.signature(Notifier.notify).parameters["local_only"]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY
    assert param.default is False


def test_perishable_is_keyword_only(tmp_path):
    """Every existing positional call site must stay untouched."""
    import inspect

    from jarvisd.notifier import Notifier
    param = inspect.signature(Notifier.notify).parameters["perishable"]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY
    assert param.default is False
