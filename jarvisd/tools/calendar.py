"""Google Calendar tool definitions and handlers.

list/get are read-only; create is a reversible write; delete is destructive
(the security guard requires confirmation before it runs).
"""

from datetime import datetime, timedelta

TOOLS = [
    {
        "name": "calendar_list_events",
        "description": (
            "List upcoming events on the user's Google Calendar over the next few days. "
            "Returns each event's id, title, start/end, and location."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": "How many days ahead to look (default 7).",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Max events to return (default 10).",
                },
            },
            "required": [],
        },
    },
    {
        "name": "calendar_get_day",
        "description": "List the events on the user's calendar for a specific day.",
        "input_schema": {
            "type": "object",
            "properties": {
                "date": {
                    "type": "string",
                    "description": "The day to look at, as YYYY-MM-DD. Defaults to today.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "calendar_create_event",
        "description": "Create an event on the user's Google Calendar.",
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "Event title."},
                "start": {"type": "string", "description": "Start datetime, ISO 8601 (e.g. '2026-10-26T12:00')."},
                "end": {"type": "string", "description": "End datetime, ISO 8601."},
                "description": {"type": "string", "description": "Optional event details."},
                "location": {"type": "string", "description": "Optional location."},
            },
            "required": ["summary", "start", "end"],
        },
    },
    {
        "name": "calendar_delete_event",
        "description": "Delete an event from the user's Google Calendar by its id (destructive — requires confirmation).",
        "input_schema": {
            "type": "object",
            "properties": {
                "event_id": {"type": "string", "description": "The event id (from calendar_list_events)."},
            },
            "required": ["event_id"],
        },
    },
]

MAX_RESULTS = 10


def _fmt_event(e):
    loc = e.get("location")
    tail = f" @ {loc}" if loc else ""
    return f"[{e.get('id')}] {e.get('start', '?')}–{e.get('end', '?')}  {e.get('summary', '(untitled)')}{tail}"


def handlers(google_client):
    """Calendar tool handlers closing over a GoogleClient."""

    def calendar_list_events(ti):
        days = ti.get("days")
        if not isinstance(days, int) or days <= 0:
            days = 7
        max_results = ti.get("max_results")
        if not isinstance(max_results, int) or max_results <= 0:
            max_results = MAX_RESULTS
        now = datetime.now()
        time_min = now.isoformat(timespec="seconds")
        time_max = (now + timedelta(days=days)).isoformat(timespec="seconds")
        try:
            events = google_client.list_events(time_min, time_max, max_results=max_results)
        except Exception as e:
            return f"Error listing calendar events: {e}"
        if not events:
            return f"No events in the next {days} day(s)."
        return "\n".join(_fmt_event(e) for e in events)

    def calendar_get_day(ti):
        date_str = ti.get("date")
        if isinstance(date_str, str) and date_str.strip():
            try:
                day = datetime.strptime(date_str.strip(), "%Y-%m-%d")
            except ValueError:
                return "Error: date must be YYYY-MM-DD."
        else:
            day = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        start = day.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        try:
            events = google_client.list_events(
                start.isoformat(timespec="seconds"),
                end.isoformat(timespec="seconds"),
                max_results=MAX_RESULTS,
            )
        except Exception as e:
            return f"Error listing calendar events: {e}"
        if not events:
            return f"No events on {start.date()}."
        return "\n".join(_fmt_event(e) for e in events)

    def calendar_create_event(ti):
        summary = ti.get("summary")
        start = ti.get("start")
        end = ti.get("end")
        for name, val in (("summary", summary), ("start", start), ("end", end)):
            if not isinstance(val, str) or not val.strip():
                return f"Error: {name} is required and must be a non-empty string."
        description = ti.get("description")
        location = ti.get("location")
        try:
            created = google_client.create_event(
                summary.strip(), start.strip(), end.strip(),
                description=description, location=location,
            )
        except Exception as e:
            return f"Error creating event: {e}"
        link = created.get("link") if isinstance(created, dict) else None
        base = f"Created event \"{summary.strip()}\" at {start.strip()}."
        return base + (f" {link}" if link else "")

    def calendar_delete_event(ti):
        event_id = ti.get("event_id")
        if not isinstance(event_id, str) or not event_id.strip():
            return "Error: event_id is required and must be a non-empty string."
        try:
            google_client.delete_event(event_id.strip())
        except Exception as e:
            return f"Error deleting event: {e}"
        return f"Deleted event {event_id.strip()}."

    return {
        "calendar_list_events": calendar_list_events,
        "calendar_get_day": calendar_get_day,
        "calendar_create_event": calendar_create_event,
        "calendar_delete_event": calendar_delete_event,
    }
