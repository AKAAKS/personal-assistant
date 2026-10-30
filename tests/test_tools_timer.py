"""set_timer tool — a one-off reminder N minutes out, reusing ReminderStore."""

from datetime import datetime

from jarvisd.tools.reminders import handlers


class FakeReminderStore:
    def __init__(self):
        self.added = []

    def add(self, text, due_ts, recurrence="none", condition=None):
        self.added.append({"text": text, "due_ts": due_ts, "recurrence": recurrence})
        return len(self.added)


def test_set_timer_creates_a_oneoff_reminder_in_the_future():
    rs = FakeReminderStore()
    out = handlers(rs)["set_timer"]({"minutes": 10})
    assert len(rs.added) == 1
    rec = rs.added[0]
    assert rec["recurrence"] == "none"
    due = datetime.fromisoformat(rec["due_ts"])
    delta_min = (due - datetime.now()).total_seconds() / 60
    assert 9 <= delta_min <= 11          # ~10 minutes out
    assert "10" in out


def test_set_timer_accepts_a_label():
    rs = FakeReminderStore()
    handlers(rs)["set_timer"]({"minutes": 5, "label": "tea"})
    assert "tea" in rs.added[0]["text"].lower()


def test_set_timer_rejects_nonpositive():
    out = handlers(FakeReminderStore())["set_timer"]({"minutes": 0})
    assert "error" in out.lower()
