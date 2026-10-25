"""Reminder tools: set, list, and cancel reminders (plus quick timers)."""

from datetime import datetime, timedelta

TOOLS = [
    {
        "name": "set_timer",
        "description": (
            "Set a quick countdown timer that goes off after a number of minutes "
            "(e.g. 'set a 10-minute timer'). A one-off reminder under the hood."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "minutes": {
                    "type": "number",
                    "description": "How many minutes until the timer goes off.",
                },
                "label": {
                    "type": "string",
                    "description": "Optional label, e.g. 'tea' or 'laundry'.",
                },
            },
            "required": ["minutes"],
        },
    },
    {
        "name": "set_reminder",
        "description": "Set a reminder for a specific time. Supports one-time, daily, or weekly recurring reminders.",
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "The reminder text (what to remind about).",
                },
                "due": {
                    "type": "string",
                    "description": "When the reminder is due (ISO 8601 datetime, e.g., '2025-01-15T14:30').",
                },
                "recurrence": {
                    "type": "string",
                    "enum": ["none", "daily", "weekly"],
                    "description": "Recurrence type: 'none' (one-time), 'daily', or 'weekly'. Default: 'none'.",
                },
                "condition": {
                    "type": "string",
                    "enum": ["meal_not_logged_today"],
                    "description": (
                        "Optional condition: 'meal_not_logged_today' makes the reminder fire only "
                        "if no meal has been logged that day (a standing nudge)."
                    ),
                },
            },
            "required": ["text", "due"],
        },
    },
    {
        "name": "list_reminders",
        "description": "List all active reminders with their IDs (use the ID with cancel_reminder).",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "cancel_reminder",
        "description": "Cancel a reminder by its ID.",
        "input_schema": {
            "type": "object",
            "properties": {
                "reminder_id": {
                    "type": "integer",
                    "description": "The ID of the reminder to cancel.",
                },
            },
            "required": ["reminder_id"],
        },
    },
]


def handlers(reminder_store):
    """Reminder tool handlers closing over reminder_store.

    Args:
        reminder_store: ReminderStore instance

    Returns:
        dict: tool name -> callable(tool_input: dict) -> str
    """

    def set_timer(ti):
        """Set a quick countdown timer (a one-off reminder N minutes out)."""
        minutes = ti.get("minutes")
        if not isinstance(minutes, (int, float)) or isinstance(minutes, bool) or minutes <= 0:
            return "Error: minutes is required and must be a positive number."
        label = ti.get("label")
        label = label.strip() if isinstance(label, str) and label.strip() else ""
        text = f"Timer: {label}" if label else "Timer's up."
        due = (datetime.now() + timedelta(minutes=float(minutes))).isoformat(timespec="seconds")
        try:
            reminder_store.add(text, due, "none")
        except ValueError as exc:
            return f"Error: {exc}"
        pretty = int(minutes) if float(minutes).is_integer() else minutes
        return f"Timer set for {pretty} minute(s)" + (f" — {label}." if label else ".")

    def set_reminder(ti):
        """Set a reminder."""
        text = ti.get("text", "").strip()
        due = ti.get("due", "").strip()
        recurrence = ti.get("recurrence", "none")
        condition = ti.get("condition") or None

        if not text:
            return "Error: reminder text is required."
        if not due:
            return "Error: due datetime is required."

        try:
            reminder_id = reminder_store.add(text, due, recurrence, condition=condition)
            return f"Reminder #{reminder_id} set for {due}"
        except ValueError as exc:
            return f"Error: {exc}"

    def list_reminders(ti):
        """List active reminders."""
        reminders = reminder_store.list_active()

        if not reminders:
            return "No active reminders."

        lines = []
        # Show the real DB id (what cancel_reminder takes), never the row
        # position -- after any cancellation they diverge and "cancel reminder
        # 1" would hit the wrong reminder.
        for reminder in reminders:
            text = reminder["text"]
            due = reminder["due_ts"]
            recurrence = reminder["recurrence"]
            recurrence_str = "" if recurrence == "none" else f" (repeats {recurrence})"
            lines.append(f"{reminder['id']}. {text} — due {due}{recurrence_str}")

        return "\n".join(lines)

    def cancel_reminder(ti):
        """Cancel a reminder."""
        reminder_id = ti.get("reminder_id")

        if reminder_id is None:
            return "Error: reminder_id is required."

        success = reminder_store.cancel(reminder_id)
        if success:
            return f"Reminder #{reminder_id} cancelled."
        else:
            return f"Error: reminder #{reminder_id} not found or already done."

    return {
        "set_timer": set_timer,
        "set_reminder": set_reminder,
        "list_reminders": list_reminders,
        "cancel_reminder": cancel_reminder,
    }
