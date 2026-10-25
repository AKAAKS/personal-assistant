"""Calendar tool handlers — list/get (read), create (write), delete (destructive)."""

from jarvisd.tools.calendar import handlers


class FakeGoogle:
    """Minimal GoogleClient stand-in for the Calendar surface."""

    def __init__(self, events=None):
        self._events = events or []
        self.created = []
        self.deleted = []
        self.list_calls = []

    def list_events(self, time_min, time_max, max_results=10):
        self.list_calls.append({"time_min": time_min, "time_max": time_max})
        return list(self._events)

    def create_event(self, summary, start, end, description=None, location=None):
        self.created.append({
            "summary": summary, "start": start, "end": end,
            "description": description, "location": location,
        })
        return {"id": "evt_1", "link": "https://calendar/evt_1"}

    def delete_event(self, event_id):
        self.deleted.append(event_id)


def _events():
    return [
        {"id": "e1", "summary": "Standup", "start": "2026-10-26T09:00", "end": "2026-10-26T09:15", "location": ""},
        {"id": "e2", "summary": "Dentist", "start": "2026-10-26T14:00", "end": "2026-10-26T15:00", "location": "Clinic"},
    ]


def test_calendar_list_events_formats_results():
    out = handlers(FakeGoogle(events=_events()))["calendar_list_events"]({})
    assert "Standup" in out and "Dentist" in out
    assert "e1" in out and "e2" in out


def test_calendar_list_events_empty():
    out = handlers(FakeGoogle())["calendar_list_events"]({})
    assert "no" in out.lower()


def test_calendar_get_day_queries_that_days_window():
    g = FakeGoogle(events=_events())
    out = handlers(g)["calendar_get_day"]({"date": "2026-10-26"})
    assert "Standup" in out
    # The window handed to the client should be bounded to the requested day.
    assert g.list_calls and g.list_calls[0]["time_min"].startswith("2026-10-26")


def test_calendar_create_event_calls_client():
    g = FakeGoogle()
    out = handlers(g)["calendar_create_event"]({
        "summary": "Lunch", "start": "2026-10-26T12:00", "end": "2026-10-26T13:00",
        "location": "Cafe",
    })
    assert g.created == [{
        "summary": "Lunch", "start": "2026-10-26T12:00", "end": "2026-10-26T13:00",
        "description": None, "location": "Cafe",
    }]
    assert "Lunch" in out


def test_calendar_delete_event_calls_client():
    g = FakeGoogle()
    out = handlers(g)["calendar_delete_event"]({"event_id": "e2"})
    assert g.deleted == ["e2"]
    assert "e2" in out or "deleted" in out.lower()


def test_calendar_create_event_requires_fields():
    out = handlers(FakeGoogle())["calendar_create_event"]({"summary": "x"})
    assert "error" in out.lower()
