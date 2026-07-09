"""Reminder tools: set, list, and cancel reminders."""

TOOLS = [
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

    def set_reminder(ti):
        """Set a reminder."""
        text = ti.get("text", "").strip()
        due = ti.get("due", "").strip()
        recurrence = ti.get("recurrence", "none")

        if not text:
            return "Error: reminder text is required."
        if not due:
            return "Error: due datetime is required."

        try:
            reminder_id = reminder_store.add(text, due, recurrence)
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
        "set_reminder": set_reminder,
        "list_reminders": list_reminders,
        "cancel_reminder": cancel_reminder,
    }
