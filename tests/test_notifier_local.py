"""The notifier must be able to interrupt the user on this PC with no client connected.

Why this file exists: on 2026-11-12 the user reported that timers and reminders
never interrupt -- they only surface as "this passed while you were away" on the
next turn. A live probe proved the server-side push works, so every missed
reminder had reached == 0: no voice client was connected when it fired. The two
fallbacks meant to cover that both failed. ha_client.send_notification defaults
to target "notify.mobile_app", which is not a real Home Assistant service (HA
answers 400 Bad Request -- logged twice), and the last resort, _enqueue, is
silent by design: it waits for the user to speak first.

So a reminder could fire with nobody to tell, and nothing on the machine the
user was sitting at would make a sound. These tests pin the local fallback that
closes that gap, and the quiet-hours exemption for timers (the default window is
23:00-08:00 and this user routinely works past 02:00, so a 2am timer was being
queued silently).
"""

from datetime import datetime

from jarvisd import db
from jarvisd.notifier import Notifier


def make_notifier(tmp_path, **kwargs):
    conn = db.connect(str(tmp_path / "jarvis.db"))
    return Notifier(conn, **kwargs), conn


def fixed_now(dt):
    return lambda: dt


class FakeHAClient:
    def __init__(self, raises=False):
        self.raises = raises
        self.calls = []

    def send_notification(self, message, title="Assistant", target="notify.mobile_app"):
        if self.raises:
            raise RuntimeError("HA unreachable")
        self.calls.append((message, title, target))
        return {}


class FakeLocal:
    """Stand-in for the Windows toast+chime fallback."""

    def __init__(self, result=True, raises=False):
        self.result = result
        self.raises = raises
        self.calls = []

    def __call__(self, text):
        if self.raises:
            raise RuntimeError("toast backend exploded")
        self.calls.append(text)
        return self.result


def queued(conn):
    return [r["text"] for r in conn.execute("SELECT text FROM notice_queue ORDER BY id")]


# --- the local fallback fires exactly when nothing better worked -------------


def test_local_fallback_used_when_no_client_is_connected(tmp_path):
    """The reported bug: reminder fires, no client, so nothing interrupted."""
    local = FakeLocal()
    n, conn = make_notifier(
        tmp_path, local_notify=local, now=fixed_now(datetime(2026, 11, 12, 14, 0))
    )
    n.push_to_clients = lambda text: 0  # nobody listening

    n.notify("Reminder: stretch", priority="high")

    assert local.calls == ["Reminder: stretch"]
    assert queued(conn) == []  # delivered, so not silently banked


def test_local_fallback_skipped_when_a_client_was_reached(tmp_path):
    """A connected client speaks the reminder aloud; a toast too would be noise."""
    local = FakeLocal()
    n, conn = make_notifier(
        tmp_path, local_notify=local, now=fixed_now(datetime(2026, 11, 12, 14, 0))
    )
    n.push_to_clients = lambda text: 1

    n.notify("Reminder: stretch", priority="high")

    assert local.calls == []
    assert queued(conn) == []


def test_local_fallback_runs_before_the_phone_push(tmp_path):
    """The user is at the PC. Make a noise here before reaching for their phone."""
    local, ha = FakeLocal(), FakeHAClient()
    n, _ = make_notifier(
        tmp_path, ha_client=ha, local_notify=local, now=fixed_now(datetime(2026, 11, 12, 14, 0))
    )
    n.push_to_clients = lambda text: 0

    n.notify("Reminder: stretch", priority="high")

    assert local.calls == ["Reminder: stretch"]
    assert ha.calls == []  # not needed; the local toast landed


def test_phone_push_still_tried_when_local_fallback_declines(tmp_path):
    """Non-Windows, or the toast backend unavailable: fall through, don't stop."""
    local, ha = FakeLocal(result=False), FakeHAClient()
    n, conn = make_notifier(
        tmp_path, ha_client=ha, local_notify=local, now=fixed_now(datetime(2026, 11, 12, 14, 0))
    )
    n.push_to_clients = lambda text: 0

    n.notify("Reminder: stretch", priority="high")

    assert len(ha.calls) == 1
    assert queued(conn) == []


def test_local_fallback_exception_never_escapes_and_still_queues(tmp_path):
    """notify() must never raise into the reminder tick, whatever a backend does."""
    local = FakeLocal(raises=True)
    n, conn = make_notifier(
        tmp_path, local_notify=local, now=fixed_now(datetime(2026, 11, 12, 14, 0))
    )
    n.push_to_clients = lambda text: 0

    n.notify("Reminder: stretch", priority="high")  # must not raise

    assert queued(conn) == ["Reminder: stretch"]  # nothing delivered -> banked


def test_low_priority_never_reaches_the_local_fallback(tmp_path):
    """Habit nudges and asides stay silent; only high/urgent may interrupt."""
    local = FakeLocal()
    n, conn = make_notifier(
        tmp_path, local_notify=local, now=fixed_now(datetime(2026, 11, 12, 14, 0))
    )
    n.push_to_clients = lambda text: 0

    n.notify("you have 2 suggestions", priority="low")

    assert local.calls == []
    assert queued(conn) == ["you have 2 suggestions"]


# --- quiet hours: timers are the exception -----------------------------------


def test_high_priority_still_respects_quiet_hours(tmp_path):
    """Unchanged: a scheduled reminder at 2am is banked, not shouted."""
    local = FakeLocal()
    n, conn = make_notifier(
        tmp_path,
        local_notify=local,
        quiet_hours="23:00-08:00",
        now=fixed_now(datetime(2026, 11, 12, 2, 0)),
    )
    n.push_to_clients = lambda text: 1

    n.notify("Reminder: clean the bathroom", priority="high")

    assert local.calls == []
    assert queued(conn) == ["Reminder: clean the bathroom"]


def test_urgent_priority_overrides_quiet_hours(tmp_path):
    """A countdown timer the user just set is something they are waiting for.

    They work past 02:00, so the 23:00-08:00 default was swallowing exactly the
    timers they were sitting there expecting.
    """
    local = FakeLocal()
    n, conn = make_notifier(
        tmp_path,
        local_notify=local,
        quiet_hours="23:00-08:00",
        now=fixed_now(datetime(2026, 11, 12, 2, 0)),
    )
    n.push_to_clients = lambda text: 0

    n.notify("Timer's up.", priority="urgent")

    assert local.calls == ["Timer's up."]
    assert queued(conn) == []


def test_urgent_priority_pushes_to_clients_during_quiet_hours(tmp_path):
    """Speaking it aloud is still the preferred route for an urgent notice."""
    reached = []
    n, conn = make_notifier(
        tmp_path, quiet_hours="23:00-08:00", now=fixed_now(datetime(2026, 11, 12, 2, 0))
    )
    n.push_to_clients = lambda text: reached.append(text) or 1

    n.notify("Timer's up.", priority="urgent")

    assert reached == ["Timer's up."]
    assert queued(conn) == []


def test_urgent_priority_queues_when_every_route_fails(tmp_path):
    """Still never lose a notice: undeliverable means banked, not dropped."""
    n, conn = make_notifier(
        tmp_path, quiet_hours="23:00-08:00", now=fixed_now(datetime(2026, 11, 12, 2, 0))
    )
    n.push_to_clients = lambda text: 0

    n.notify("Timer's up.", priority="urgent")

    assert queued(conn) == ["Timer's up."]
