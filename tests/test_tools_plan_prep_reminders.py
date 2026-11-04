from datetime import date, datetime, timedelta

from jarvisd.reminder_store import ReminderStore
from jarvisd.tools import prep
from tests.test_tools_plan_prep import PrepBase

D3 = (date.today() + timedelta(days=3)).isoformat()
D5 = (date.today() + timedelta(days=5)).isoformat()


def _minus1(day):
    return (date.fromisoformat(day) - timedelta(days=1)).isoformat()


class TestReminders(PrepBase):
    def reminders(self):
        return ReminderStore(self.conn).list_active()

    def test_prep_reminder_day_before(self):
        out = self.run_tool({"dishes": [self.dish("Chicken bowl", [self.chicken(100)], D3)]})
        rs = self.reminders()
        assert len(rs) == 1
        assert rs[0]["due_ts"].startswith(_minus1(D3))
        assert "Chicken bowl" in rs[0]["text"] and D3 in rs[0]["text"]
        assert "eminder" in out

    def test_shopping_reminder_once_at_earliest(self):
        # chicken 300+300=600 vs 500 -> buy 100
        self.run_tool({"dishes": [self.dish("A", [self.chicken(300)], D5),
                                  self.dish("B", [self.chicken(300)], D3)]})
        rs = self.reminders()
        shop = [r for r in rs if "hopping" in r["text"]]
        prep_rs = [r for r in rs if "hopping" not in r["text"]]
        assert len(shop) == 1 and len(prep_rs) == 2
        assert shop[0]["due_ts"].startswith(_minus1(D3))
        assert "Chicken" in shop[0]["text"] and "100" in shop[0]["text"]

    def test_no_shopping_reminder_when_stocked(self):
        self.run_tool({"dishes": [self.dish("A", [self.chicken(100)], D3)]})
        rs = self.reminders()
        assert len(rs) == 1 and "hopping" not in rs[0]["text"]

    def test_two_dates_two_prep_reminders(self):
        self.run_tool({"dishes": [self.dish("A", [self.chicken(100)], D3),
                                  self.dish("B", [self.rice(10)], D5)]})
        dues = sorted(r["due_ts"][:10] for r in self.reminders())
        assert dues == [_minus1(D3), _minus1(D5)]

    def test_today_clamped_no_crash(self):
        before = datetime.now().replace(microsecond=0)
        out = self.run_tool({"dishes": [self.dish("A", [self.chicken(100)],
                                                  date.today().isoformat())]})
        rs = self.reminders()
        assert len(rs) == 1
        assert datetime.fromisoformat(rs[0]["due_ts"]) >= before
        assert not out.startswith("Error")

    def test_reminder_failure_contained(self, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("x")
        monkeypatch.setattr(prep.ReminderStore, "add", boom)
        out = self.run_tool({"dishes": [self.dish("A", [self.chicken(100)], D3)]})
        assert "Locked" in out and "couldn't set" in out


def test_day_before_normal():
    now = datetime(2026, 11, 3, 9, 0, 0)
    assert prep._day_before("2026-11-06", now=now) == "2026-11-05T18:00:00"


def test_day_before_clamps_past():
    now = datetime(2026, 11, 3, 9, 0, 0)
    assert prep._day_before("2026-11-03", now=now) == "2026-11-03T09:00:00"
    assert prep._day_before("2026-10-01", now=now) == "2026-11-03T09:00:00"
