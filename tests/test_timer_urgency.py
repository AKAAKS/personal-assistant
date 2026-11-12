"""A countdown timer must sound during quiet hours; a scheduled reminder must not.

The default quiet window is 23:00-08:00, and this user routinely works past
02:00 -- so a timer they set at 2am and were actively waiting on was queued
silently, which defeats the point of a timer. A reminder to clean the bathroom
at 2am is a different thing entirely and should still be banked.

Telling them apart needs the store to remember which it is, so `kind` rides on
the row rather than being guessed from the reminder text.
"""

import asyncio
from datetime import datetime

from jarvisd import db
from jarvisd.notifier import Notifier
from jarvisd.reminder_store import ReminderStore
from jarvisd.server import JarvisServer
from jarvisd.tools.reminders import handlers as build_reminder_tools


class FakeAssistant:
    conversation: list = []

    def chat(self, text):
        return ""


def store(tmp_path):
    conn = db.connect(str(tmp_path / "jarvis.db"))
    return ReminderStore(conn), conn


# --- the row remembers what it is ------------------------------------------


def test_add_defaults_to_kind_reminder(tmp_path):
    s, conn = store(tmp_path)
    rid = s.add("clean the bathroom", "2026-11-12T13:00:00")
    row = conn.execute("SELECT kind FROM reminders WHERE id=?", (rid,)).fetchone()
    assert row["kind"] == "reminder"


def test_add_records_an_explicit_kind(tmp_path):
    s, conn = store(tmp_path)
    rid = s.add("Timer's up.", "2026-11-12T13:00:00", kind="timer")
    row = conn.execute("SELECT kind FROM reminders WHERE id=?", (rid,)).fetchone()
    assert row["kind"] == "timer"


def test_due_rows_expose_kind(tmp_path):
    """_fire_due_reminders reads it off the row it already has."""
    s, _ = store(tmp_path)
    s.add("Timer's up.", "2000-01-01T00:00:00", kind="timer")
    due = s.due(datetime.now())
    assert [r["kind"] for r in due] == ["timer"]


def test_pre_existing_rows_read_as_reminders(tmp_path):
    """The migration backfills rather than leaving NULL: existing reminders must
    keep respecting quiet hours, not silently become urgent."""
    s, conn = store(tmp_path)
    conn.execute(
        "INSERT INTO reminders (text, due_ts, recurrence, status, created_ts) "
        "VALUES ('legacy', '2000-01-01T00:00:00', 'none', 'active', '2000-01-01T00:00:00')"
    )
    conn.commit()
    due = s.due(datetime.now())
    assert [r["kind"] for r in due] == ["reminder"]


# --- the tools set it ------------------------------------------------------


def test_set_timer_records_kind_timer(tmp_path):
    s, conn = store(tmp_path)
    tools = build_reminder_tools(s)
    tools["set_timer"]({"minutes": 10})
    row = conn.execute("SELECT kind FROM reminders ORDER BY id DESC LIMIT 1").fetchone()
    assert row["kind"] == "timer"


def test_set_reminder_records_kind_reminder(tmp_path):
    s, conn = store(tmp_path)
    tools = build_reminder_tools(s)
    tools["set_reminder"]({"text": "clean the bathroom", "due": "2026-11-13T13:00:00"})
    row = conn.execute("SELECT kind FROM reminders ORDER BY id DESC LIMIT 1").fetchone()
    assert row["kind"] == "reminder"


# --- and the tick routes on it --------------------------------------------


def test_timer_sounds_during_quiet_hours(tmp_path):
    """The reported gap: a 2am timer must not be banked."""
    s, conn = store(tmp_path)
    notifier = Notifier(
        conn, quiet_hours="23:00-08:00", now=lambda: datetime(2026, 11, 12, 2, 0)
    )
    spoken = []
    notifier.push_to_clients = lambda text: spoken.append(text) or 1
    s.add("Timer's up.", "2000-01-01T00:00:00", kind="timer")

    server = JarvisServer(FakeAssistant(), notifier=notifier, reminder_store=s)
    asyncio.run(server._fire_due_reminders())

    assert spoken == ["Reminder: Timer's up."]
    assert notifier.drain() == []  # delivered, not banked


def test_scheduled_reminder_still_queues_during_quiet_hours(tmp_path):
    s, conn = store(tmp_path)
    notifier = Notifier(
        conn, quiet_hours="23:00-08:00", now=lambda: datetime(2026, 11, 12, 2, 0)
    )
    spoken = []
    notifier.push_to_clients = lambda text: spoken.append(text) or 1
    s.add("clean the bathroom", "2000-01-01T00:00:00")

    server = JarvisServer(FakeAssistant(), notifier=notifier, reminder_store=s)
    asyncio.run(server._fire_due_reminders())

    assert spoken == []
    assert notifier.drain() == ["Reminder: clean the bathroom"]


def test_timer_outside_quiet_hours_behaves_normally(tmp_path):
    s, conn = store(tmp_path)
    notifier = Notifier(
        conn, quiet_hours="23:00-08:00", now=lambda: datetime(2026, 11, 12, 14, 0)
    )
    spoken = []
    notifier.push_to_clients = lambda text: spoken.append(text) or 1
    s.add("Timer's up.", "2000-01-01T00:00:00", kind="timer")

    server = JarvisServer(FakeAssistant(), notifier=notifier, reminder_store=s)
    asyncio.run(server._fire_due_reminders())

    assert spoken == ["Reminder: Timer's up."]
