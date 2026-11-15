"""Alarm mode: a notice the user is actively waiting on rings every channel.

Why this file exists: a 20-minute timer fired on schedule and was spoken once,
for 2.3 seconds, and the user missed it. Notifier.notify returned on the first
channel that succeeded, so the toast and chime never ran. That is right for a
nudge and wrong for an alarm.
"""

from datetime import datetime

import pytest

from jarvisd import db
from jarvisd.notifier import Notifier

TEXT = "Timer's up."


class Spy:
    """One object recording what every channel was asked to deliver."""

    def __init__(self):
        self.pushed = []
        self.local = []
        self.ha = []
        self.push_result = 1
        self.local_raises = False
        self.ha_raises = False

    def push_to_clients(self, text):
        self.pushed.append(text)
        return self.push_result

    def local_notify(self, text):
        if self.local_raises:
            raise RuntimeError("toast backend exploded")
        self.local.append(text)
        return True

    def send_notification(self, message, title="Assistant", target="notify.mobile_app"):
        if self.ha_raises:
            raise RuntimeError("HA unreachable")
        self.ha.append(message)
        return {}


@pytest.fixture
def conn(tmp_path):
    return db.connect(str(tmp_path / "jarvis.db"))


@pytest.fixture
def spy():
    return Spy()


def _wire(conn, spy, now):
    n = Notifier(conn, ha_client=spy, now=lambda: now, local_notify=spy.local_notify)
    n.push_to_clients = spy.push_to_clients
    return n


@pytest.fixture
def notifier(conn, spy):
    return _wire(conn, spy, datetime(2026, 11, 15, 14, 0))


@pytest.fixture
def notifier_in_quiet_hours(conn, spy):
    return _wire(conn, spy, datetime(2026, 11, 15, 2, 0))  # default window 23:00-08:00


def queued(conn):
    return conn.execute("SELECT COUNT(*) c FROM notice_queue").fetchone()["c"]


def test_alarm_rings_every_channel_even_when_a_client_was_reached(notifier, spy):
    # THE BUG: notify returned on the first success, so a timer that was spoken
    # never produced the chime or the toast. The user heard one 2.3s line.
    notifier.notify(TEXT, "urgent", alarm=True)
    assert spy.pushed == [TEXT]
    assert spy.local == [TEXT]
    assert spy.ha == [TEXT]


def test_a_non_alarm_notice_still_stops_at_the_first_channel(notifier, spy):
    notifier.notify("Reminder: bins", "high")
    assert spy.pushed and not spy.local and not spy.ha


def test_alarm_still_lands_locally_when_no_client_is_connected(notifier, spy):
    spy.push_result = 0
    notifier.notify(TEXT, "urgent", alarm=True)
    assert spy.local == [TEXT]


def test_alarm_is_queued_only_when_every_channel_fails(notifier, spy, conn):
    spy.push_result = 0
    spy.local_raises = spy.ha_raises = True
    notifier.notify(TEXT, "urgent", alarm=True)
    assert queued(conn) == 1


def test_alarm_is_not_queued_when_any_one_channel_succeeded(notifier, spy, conn):
    spy.push_result = 0
    spy.local_raises = True
    notifier.notify(TEXT, "urgent", alarm=True)  # only the HA push lands
    assert spy.ha == [TEXT]
    assert queued(conn) == 0


def test_one_failing_channel_does_not_stop_the_others(notifier, spy):
    spy.local_raises = True
    notifier.notify(TEXT, "urgent", alarm=True)
    assert spy.pushed and spy.ha


def test_alarm_with_local_only_skips_the_phone_push(notifier, spy):
    notifier.notify(TEXT, "urgent", alarm=True, local_only=True)
    assert spy.pushed and spy.local and not spy.ha


def test_a_perishable_alarm_with_no_route_is_dropped_not_queued(notifier, spy, conn):
    spy.push_result = 0
    spy.local_raises = spy.ha_raises = True
    notifier.notify(TEXT, "urgent", alarm=True, perishable=True)
    assert queued(conn) == 0


def test_an_urgent_alarm_ignores_quiet_hours(notifier_in_quiet_hours, spy):
    notifier_in_quiet_hours.notify(TEXT, "urgent", alarm=True)
    assert spy.local == [TEXT]


def test_a_high_alarm_still_respects_quiet_hours(notifier_in_quiet_hours, spy, conn):
    notifier_in_quiet_hours.notify(TEXT, "high", alarm=True)
    assert not spy.pushed and not spy.local and not spy.ha
    assert queued(conn) == 1
