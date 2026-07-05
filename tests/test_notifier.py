import logging
from datetime import datetime

import pytest

from jarvisd import db
from jarvisd.notifier import Notifier


def make_notifier(tmp_path, **kwargs):
    conn = db.connect(str(tmp_path / "jarvis.db"))
    notifier = Notifier(conn, **kwargs)
    return notifier, conn


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


# --- quiet-hours parsing ---


def test_quiet_hours_overnight_wrap_quiet_at_2330(tmp_path):
    n, conn = make_notifier(
        tmp_path, quiet_hours="23:00-08:00", now=fixed_now(datetime(2026, 7, 5, 23, 30))
    )
    n.notify("hi", priority="high")
    conn2 = conn
    rows = conn2.execute("SELECT * FROM notice_queue").fetchall()
    assert len(rows) == 1
    assert rows[0]["delivered"] == 0


def test_quiet_hours_overnight_wrap_quiet_at_0759(tmp_path):
    n, conn = make_notifier(
        tmp_path, quiet_hours="23:00-08:00", now=fixed_now(datetime(2026, 7, 5, 7, 59))
    )
    n.notify("hi", priority="high")
    rows = conn.execute("SELECT * FROM notice_queue").fetchall()
    assert len(rows) == 1


def test_quiet_hours_overnight_wrap_not_quiet_at_1200(tmp_path):
    n, conn = make_notifier(
        tmp_path, quiet_hours="23:00-08:00", now=fixed_now(datetime(2026, 7, 5, 12, 0))
    )
    reached = {"n": 0}

    def push(text):
        reached["n"] += 1
        return 1

    n.push_to_clients = push
    n.notify("hi", priority="high")
    rows = conn.execute("SELECT * FROM notice_queue").fetchall()
    assert len(rows) == 0  # delivered via push, not queued
    assert reached["n"] == 1


def test_quiet_hours_start_boundary_inclusive(tmp_path):
    n, conn = make_notifier(
        tmp_path, quiet_hours="23:00-08:00", now=fixed_now(datetime(2026, 7, 5, 23, 0))
    )
    n.notify("hi", priority="high")
    rows = conn.execute("SELECT * FROM notice_queue").fetchall()
    assert len(rows) == 1


def test_quiet_hours_end_boundary_exclusive(tmp_path):
    n, conn = make_notifier(
        tmp_path, quiet_hours="23:00-08:00", now=fixed_now(datetime(2026, 7, 5, 8, 0))
    )
    n.push_to_clients = lambda text: 1
    n.notify("hi", priority="high")
    rows = conn.execute("SELECT * FROM notice_queue").fetchall()
    assert len(rows) == 0  # 08:00 is not quiet, delivered via push instead


def test_empty_quiet_hours_never_quiet(tmp_path):
    n, conn = make_notifier(
        tmp_path, quiet_hours="", now=fixed_now(datetime(2026, 7, 5, 23, 30))
    )
    n.push_to_clients = lambda text: 1
    n.notify("hi", priority="high")
    rows = conn.execute("SELECT * FROM notice_queue").fetchall()
    assert len(rows) == 0


def test_none_quiet_hours_never_quiet(tmp_path):
    n, conn = make_notifier(
        tmp_path, quiet_hours=None, now=fixed_now(datetime(2026, 7, 5, 23, 30))
    )
    n.notify("hi", priority="low")
    rows = conn.execute("SELECT * FROM notice_queue").fetchall()
    assert len(rows) == 1  # low priority always queues regardless of quiet hours


def test_malformed_quiet_hours_logs_warning_and_disables(tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        n, conn = make_notifier(
            tmp_path, quiet_hours="garbage", now=fixed_now(datetime(2026, 7, 5, 23, 30))
        )
    assert any("quiet" in r.message.lower() for r in caplog.records)
    n.push_to_clients = lambda text: 1
    n.notify("hi", priority="high")
    rows = conn.execute("SELECT * FROM notice_queue").fetchall()
    assert len(rows) == 0  # disabled quiet hours -> delivered via push


def test_quiet_hours_active_enqueues_even_for_high_priority_ignoring_push_and_ha(tmp_path):
    ha = FakeHAClient()
    n, conn = make_notifier(
        tmp_path,
        ha_client=ha,
        quiet_hours="23:00-08:00",
        now=fixed_now(datetime(2026, 7, 5, 23, 30)),
    )
    n.push_to_clients = lambda text: 5
    n.notify("hi", priority="high")
    rows = conn.execute("SELECT * FROM notice_queue").fetchall()
    assert len(rows) == 1
    assert ha.calls == []


# --- high priority decision chain ---


def test_high_priority_delivers_via_push_to_clients_when_reached(tmp_path):
    ha = FakeHAClient()
    n, conn = make_notifier(
        tmp_path, ha_client=ha, quiet_hours="", now=fixed_now(datetime(2026, 7, 5, 12, 0))
    )
    n.push_to_clients = lambda text: 2
    n.notify("hi", priority="high")
    rows = conn.execute("SELECT * FROM notice_queue").fetchall()
    assert len(rows) == 0
    assert ha.calls == []  # HA fallback not used when push reached clients


def test_high_priority_falls_back_to_ha_when_zero_clients(tmp_path):
    ha = FakeHAClient()
    n, conn = make_notifier(
        tmp_path, ha_client=ha, quiet_hours="", now=fixed_now(datetime(2026, 7, 5, 12, 0))
    )
    n.push_to_clients = lambda text: 0
    n.notify("hi", priority="high")
    rows = conn.execute("SELECT * FROM notice_queue").fetchall()
    assert len(rows) == 0  # delivered via HA
    assert ha.calls == [("hi", "JARVIS", "notify.mobile_app")]


def test_high_priority_falls_back_to_ha_when_push_to_clients_none(tmp_path):
    ha = FakeHAClient()
    n, conn = make_notifier(
        tmp_path, ha_client=ha, quiet_hours="", now=fixed_now(datetime(2026, 7, 5, 12, 0))
    )
    n.notify("hi", priority="high")
    rows = conn.execute("SELECT * FROM notice_queue").fetchall()
    assert len(rows) == 0
    assert ha.calls == [("hi", "JARVIS", "notify.mobile_app")]


def test_high_priority_push_to_clients_exception_treated_as_zero(tmp_path):
    ha = FakeHAClient()

    def broken_push(text):
        raise RuntimeError("client dropped mid-send")

    n, conn = make_notifier(
        tmp_path, ha_client=ha, quiet_hours="", now=fixed_now(datetime(2026, 7, 5, 12, 0))
    )
    n.push_to_clients = broken_push
    n.notify("hi", priority="high")
    rows = conn.execute("SELECT * FROM notice_queue").fetchall()
    assert len(rows) == 0
    assert ha.calls == [("hi", "JARVIS", "notify.mobile_app")]


def test_high_priority_enqueues_when_both_unavailable(tmp_path):
    n, conn = make_notifier(
        tmp_path, ha_client=None, quiet_hours="", now=fixed_now(datetime(2026, 7, 5, 12, 0))
    )
    n.notify("hi", priority="high")
    rows = conn.execute("SELECT * FROM notice_queue").fetchall()
    assert len(rows) == 1
    assert rows[0]["text"] == "hi"
    assert rows[0]["delivered"] == 0


def test_high_priority_enqueues_when_both_fail(tmp_path):
    ha = FakeHAClient(raises=True)
    n, conn = make_notifier(
        tmp_path, ha_client=ha, quiet_hours="", now=fixed_now(datetime(2026, 7, 5, 12, 0))
    )
    n.push_to_clients = lambda text: 0
    n.notify("hi", priority="high")
    rows = conn.execute("SELECT * FROM notice_queue").fetchall()
    assert len(rows) == 1
    assert rows[0]["text"] == "hi"


def test_high_priority_enqueue_ts_uses_injected_now(tmp_path):
    when = datetime(2026, 7, 5, 12, 0, 0)
    n, conn = make_notifier(
        tmp_path, ha_client=None, quiet_hours="", now=fixed_now(when)
    )
    n.notify("hi", priority="high")
    rows = conn.execute("SELECT * FROM notice_queue").fetchall()
    assert rows[0]["ts"] == when.isoformat(timespec="seconds")


# --- low priority ---


def test_low_priority_always_enqueues_even_with_clients_and_ha(tmp_path):
    ha = FakeHAClient()
    n, conn = make_notifier(
        tmp_path, ha_client=ha, quiet_hours="", now=fixed_now(datetime(2026, 7, 5, 12, 0))
    )
    n.push_to_clients = lambda text: 5
    n.notify("hi", priority="low")
    rows = conn.execute("SELECT * FROM notice_queue").fetchall()
    assert len(rows) == 1
    assert ha.calls == []


def test_default_priority_is_low(tmp_path):
    n, conn = make_notifier(
        tmp_path, quiet_hours="", now=fixed_now(datetime(2026, 7, 5, 12, 0))
    )
    n.notify("hi")
    rows = conn.execute("SELECT * FROM notice_queue").fetchall()
    assert len(rows) == 1


# --- drain ---


def test_drain_returns_undelivered_oldest_first_and_marks_delivered(tmp_path):
    n, conn = make_notifier(
        tmp_path, quiet_hours="", now=fixed_now(datetime(2026, 7, 5, 12, 0))
    )
    n.notify("first", priority="low")
    n.notify("second", priority="low")
    texts = n.drain()
    assert texts == ["first", "second"]
    rows = conn.execute("SELECT delivered FROM notice_queue").fetchall()
    assert all(r["delivered"] == 1 for r in rows)


def test_drain_idempotent_when_empty(tmp_path):
    n, conn = make_notifier(
        tmp_path, quiet_hours="", now=fixed_now(datetime(2026, 7, 5, 12, 0))
    )
    assert n.drain() == []
    n.notify("first", priority="low")
    n.drain()
    assert n.drain() == []  # already delivered, nothing left


def test_drain_does_not_return_already_delivered_notices(tmp_path):
    n, conn = make_notifier(
        tmp_path, quiet_hours="", now=fixed_now(datetime(2026, 7, 5, 12, 0))
    )
    n.notify("first", priority="low")
    n.drain()
    n.notify("second", priority="low")
    assert n.drain() == ["second"]


# --- notify never raises ---


def test_notify_never_raises_when_conn_broken(tmp_path):
    n, conn = make_notifier(
        tmp_path, quiet_hours="", now=fixed_now(datetime(2026, 7, 5, 12, 0))
    )
    conn.close()
    n.notify("hi", priority="low")  # must not raise
    n.notify("hi", priority="high")  # must not raise


def test_drain_never_raises_when_conn_broken(tmp_path):
    n, conn = make_notifier(
        tmp_path, quiet_hours="", now=fixed_now(datetime(2026, 7, 5, 12, 0))
    )
    conn.close()
    assert n.drain() == []
