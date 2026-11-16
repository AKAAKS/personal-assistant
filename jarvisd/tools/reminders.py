"""Reminder and timer tools.

Reminders: set, list and cancel (ReminderStore). Timers: set_timer starts a countdown in
TimerStore, timer_status reports live and recently fired timers, and timer_control
pauses, resumes, extends, cancels, dismisses, snoozes or renames one.
"""

import math
from datetime import datetime

from jarvisd.reminder_store import BAD_DUE_TS, normalize_due_ts, parse_recurrence
from jarvisd.text_format import speak_due, speak_duration, speak_recurrence
from jarvisd.timer_store import parse_ts, remaining_for, to_naive

_ACTIONS = ("pause", "resume", "extend", "cancel", "dismiss", "snooze", "rename")
_DEFAULT_SNOOZE_MIN = 5

# The timer tools in TOOLS below. build_registry gates these on the timer store and
# every other tool in TOOLS on the reminder store, so a new tool must be classified:
# list it here if it needs timers, or in REMINDER_TOOL_NAMES if it needs reminders.
TIMER_TOOL_NAMES = frozenset({"set_timer", "timer_status", "timer_control"})
REMINDER_TOOL_NAMES = frozenset({"set_reminder", "list_reminders", "cancel_reminder"})

TOOLS = [
    {
        "name": "set_timer",
        "description": (
            "Start a countdown timer (e.g. 'set a 10-minute timer', 'a 90 second timer for the eggs'). "
            "It rings on every device until the user dismisses it. Returns the timer's id; "
            "give it a label when the user names it ('pasta') so it can be referred to later."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "minutes": {
                    "type": "number",
                    "description": "Minutes until the timer goes off (use 0 with 'seconds' for a seconds-only timer).",
                },
                "seconds": {
                    "type": "number",
                    "description": "Optional extra seconds added to the minutes.",
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
        "name": "timer_status",
        "description": (
            "Check the user's timers. ALWAYS call this to answer 'do I have a timer on?', "
            "'is my timer done?', 'did it go off?' or 'how long is left?' -- never answer from memory. "
            "Lists running, paused and still-ringing timers with the time remaining, plus timers "
            "that went off in the last 30 minutes."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "timer_control",
        "description": (
            "Pause, resume, extend, cancel, dismiss, snooze or rename a timer. Name the timer by its "
            "label ('pause the pasta timer') or its timer_id; if neither is given and only one "
            "timer is live, that one is used. 'dismiss' stops a ringing timer (and also ends a running or paused one); "
            "'snooze' makes a ringing timer ring again after 'minutes' (default 5); 'extend' adds 'minutes' "
            "to a timer; 'rename' gives a live timer the name in 'new_label' (not one another live timer already has). "
            "'label' only ever SELECTS a timer, so to name or rename one use action 'rename' with 'new_label' "
            "-- no other action changes a label, and a 'label' that does not match the timer is an error. "
            "If several timers match, it lists them -- ask the user which."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": list(_ACTIONS),
                    "description": "What to do to the timer.",
                },
                "timer_id": {
                    "type": "integer",
                    "description": "The timer's id (from set_timer or timer_status).",
                },
                "label": {
                    "type": "string",
                    "description": (
                        "The label of the timer to act on, e.g. 'pasta'. Alternative to timer_id. "
                        "It never renames a timer: to name one, use action 'rename' with new_label."
                    ),
                },
                "new_label": {
                    "type": "string",
                    "description": "rename only: the timer's new name, e.g. 'trash'.",
                },
                "minutes": {
                    "type": "number",
                    "description": "Minutes to add (extend, required) or to snooze for (snooze, default 5).",
                },
            },
            "required": ["action"],
        },
    },
    {
        "name": "set_reminder",
        "description": "Set a reminder for a specific time. Supports one-time reminders and recurring ones: daily, weekdays (Mon-Fri), weekends (Sat-Sun), weekly, or monthly.",
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
                    # No `enum`: an enum would forbid the parameterised forms.
                    "pattern": (
                        "^(none|daily|weekdays|weekends|weekly|monthly"
                        "|monthly:([1-9]|[12][0-9]|3[01])"
                        "|dow:[0-6](,[0-6])*)$"
                    ),
                    "description": (
                        "How the reminder repeats. Plain values: 'none' (one-time, the default), "
                        "'daily', 'weekdays' (Mon-Fri), 'weekends' (Sat-Sun), 'weekly', "
                        "'monthly' (same day of the month; clamped to the last day in shorter months). "
                        "For specific days of the week use 'dow:' followed by comma-separated "
                        "numbers where Monday=0, Tuesday=1, Wednesday=2, Thursday=3, Friday=4, "
                        "Saturday=5, Sunday=6, e.g. 'dow:0,2,4' for every Monday, Wednesday and Friday."
                    ),
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


def _name(row):
    return row["label"] or "unnamed timer"


def handlers(reminder_store, timer_store=None, clock=datetime.now):
    """Reminder and timer tool handlers.

    Args:
        reminder_store: ReminderStore instance
        timer_store: TimerStore instance; without it the timer tools report that
            timers are unavailable (existing reminder-only call sites keep working)
        clock: returns the current local time (injectable for tests)

    Returns:
        dict: tool name -> callable(tool_input: dict) -> str
    """

    def _describe(row, now):
        """One spoken line about a live timer: label, id, state and time."""
        name = _name(row)
        try:
            rem = remaining_for(row, now)
        except BAD_DUE_TS:
            return f"{name} (#{row['id']}) has an unreadable time."
        if row["status"] == "ringing":
            return f"{name} (#{row['id']}) is ringing now."
        if row["status"] == "paused":
            return f"{name} (#{row['id']}) is paused with {speak_duration(rem)} left."
        if rem == 0:  # due, not yet ringing; speak_duration(0) would say "no time left"
            return f"{name} (#{row['id']}) is due now."
        return f"{name} (#{row['id']}) has {speak_duration(rem)} left."

    def _fired_line(row, now):
        name = _name(row)
        try:
            ago = int((to_naive(now) - parse_ts(row["fired_ts"])).total_seconds())
        except BAD_DUE_TS:
            return f"{name} (#{row['id']}) went off recently."
        return f"{name} (#{row['id']}) went off {speak_duration(max(ago, 1))} ago and was dismissed."

    def set_timer(ti):
        """Start a countdown timer."""
        if timer_store is None:
            return "Error: timers are not available right now."
        minutes = ti.get("minutes")
        seconds = ti.get("seconds")
        if seconds is None:  # models routinely send null for an optional field
            seconds = 0
        for v in (minutes, seconds):
            if (not isinstance(v, (int, float)) or isinstance(v, bool)
                    or not math.isfinite(v) or v < 0):
                return "Error: minutes is required, and minutes and seconds must be finite numbers that are not negative."
        total = round(float(minutes) * 60 + float(seconds))
        if total <= 0:
            return "Error: the timer must be longer than zero."
        label = ti.get("label")
        label = label.strip() if isinstance(label, str) and label.strip() else ""
        try:
            timer_id = timer_store.add(total, label, now=clock())
        except (ValueError, OSError, OverflowError) as exc:
            return f"Error: {exc}"
        return (f"Timer #{timer_id} set for {speak_duration(total)}"
                + (f" - {label}." if label else "."))

    def timer_status(ti):
        """Report live timers and ones that recently went off."""
        if timer_store is None:
            return "Error: timers are not available right now."
        now = clock()
        lines = [_describe(r, now) for r in timer_store.list_live(now)]
        # A still-ringing timer is already reported above as live.
        lines += [_fired_line(r, now) for r in timer_store.recently_fired(now)
                  if r["status"] != "ringing"]
        if not lines:
            return "No timers are running and none went off recently."
        return "\n".join(lines)

    def _miss(message, now):
        """A selector matched nothing. If a timer is ringing, say so: a failed
        "stop it" must never leave the model believing there is nothing to stop."""
        ringing = [r for r in timer_store.list_live(now) if r["status"] == "ringing"]
        return " ".join([message] + [_describe(r, now) for r in ringing])

    def _resolve(timer_id, label, action, now):
        """Return (row, None) for the target timer, or (None, message).

        label is a selector here, already stripped ('' when absent). Given with a
        timer_id it must agree with that timer: a label that plays no part in the
        outcome is reported, never silently dropped.
        """
        if timer_id is not None:
            if not isinstance(timer_id, int) or isinstance(timer_id, bool):
                return None, "Error: timer_id must be an integer."
            row = timer_store.get(timer_id)
            if row is None or row["status"] not in ("running", "paused", "ringing"):
                return None, _miss(f"No live timer #{timer_id}.", now)
            if label and row["label"].casefold() != label.casefold():
                return None, (f"Error: timer #{timer_id} is {_name(row)}, not {label!r}. "
                              "Nothing was changed. To rename a timer use action 'rename' with new_label.")
            return row, None
        if label:
            matches = timer_store.find_by_label(label)
            if not matches:
                return None, _miss(f"No live timer is labelled {label!r}.", now)
        else:
            matches = timer_store.list_live(now)
            if not matches:
                return None, "No timers are running."
            if len(matches) > 1 and action in ("dismiss", "snooze"):
                # An untargeted "stop it" means the timer that is making noise.
                # Prefer a lone ringing one (dismiss also works on running and
                # paused timers, but those are not what the user is hearing).
                # Two or more ringing timers stay ambiguous and are listed.
                ringing = [r for r in matches if r["status"] == "ringing"]
                if len(ringing) == 1:
                    matches = ringing
        if len(matches) > 1:
            listing = " ".join(_describe(r, now) for r in matches)
            return None, f"Which timer? Several match. {listing}"
        return matches[0], None

    # What a huge `minutes` raises: round(inf), timedelta, datetime + timedelta,
    # and an int too big for SQLite. The same set set_timer guards.
    _TOO_LARGE = (ValueError, OSError, OverflowError)

    def _too_large(action):
        return f"Error: minutes is too large to {action} by."

    def timer_control(ti):
        """Pause, resume, extend, cancel, dismiss, snooze or rename a timer."""
        if timer_store is None:
            return "Error: timers are not available right now."
        action = ti.get("action")
        if action not in _ACTIONS:
            return f"Error: action must be one of {', '.join(_ACTIONS)}."
        timer_id = ti.get("timer_id")
        label = ti.get("label")
        if label is not None and not isinstance(label, str):
            return "Error: label must be a string."
        label = label.strip() if label else ""
        new_label = ti.get("new_label")
        if action == "rename":
            if new_label is None and timer_id is not None and label:
                # What the model emits for "name it X": timer_id has already
                # selected the timer, so the label can only be the new name.
                new_label, label = label, ""
            elif new_label is None and label:
                return ("Error: rename needs new_label. A bare label only selects a timer, "
                        "so say which name to give it with new_label.")
            if not isinstance(new_label, str) or not new_label.strip():
                return "Error: rename needs new_label, the timer's new name (a non-blank string)."
            new_label = new_label.strip()
        elif new_label is not None:
            return f"Error: new_label only applies to action 'rename', not {action!r}."
        minutes = ti.get("minutes")
        # minutes matters only to extend (required) and snooze (optional);
        # a stray value alongside any other action is ignored.
        if action == "extend" or (action == "snooze" and minutes is not None):
            if (not isinstance(minutes, (int, float)) or isinstance(minutes, bool)
                    or not math.isfinite(minutes) or minutes <= 0):
                return f"Error: {action} needs minutes, a positive number."
        now = clock()
        row, problem = _resolve(timer_id, label, action, now)
        if row is None:
            return problem
        tid, name = row["id"], _name(row)
        if action == "pause":
            if timer_store.pause(tid, now):
                return f"Paused {name} (#{tid})."
            return f"Couldn't pause {name} (#{tid}); it isn't running."
        if action == "resume":
            if timer_store.resume(tid, now):
                return f"Resumed {name} (#{tid})."
            return f"Couldn't resume {name} (#{tid}); it isn't paused."
        if action == "extend":
            try:
                extra = round(float(minutes) * 60)
                extended = timer_store.extend(tid, extra, now)
            except _TOO_LARGE:
                return _too_large(action)
            if not extended:
                return f"Couldn't extend {name} (#{tid}); it isn't running or paused."
            return f"Added {speak_duration(extra)} to {name} (#{tid}). {_describe(timer_store.get(tid), now)}"
        if action == "rename":
            # One live timer per label, as for stopwatches: a duplicate would make
            # every later label selection ambiguous. Finished timers don't count.
            for other in timer_store.find_by_label(new_label):
                if other["id"] != tid:
                    return (f"Error: {_name(other)} (#{other['id']}) already has that name. "
                            "Pick a different name.")
            if timer_store.rename(tid, new_label):
                return f"Renamed {name} (#{tid}) to {new_label!r}."
            return f"Couldn't rename {name} (#{tid}); it is already finished."
        if action == "cancel":
            if timer_store.cancel(tid):
                return f"Cancelled {name} (#{tid})."
            return f"Couldn't cancel {name} (#{tid}); it is already finished."
        if action == "dismiss":
            if timer_store.dismiss(tid):
                return f"Dismissed {name} (#{tid})."
            return f"Couldn't dismiss {name} (#{tid}); it is already finished."
        try:
            snooze_sec = round((float(minutes) if minutes is not None else _DEFAULT_SNOOZE_MIN) * 60)
            snoozed = timer_store.snooze(tid, snooze_sec, now)
        except _TOO_LARGE:
            return _too_large(action)
        if snoozed:
            return f"Snoozed {name} (#{tid}) for {speak_duration(snooze_sec)}."
        return f"Couldn't snooze {name} (#{tid}); it isn't ringing."

    def set_reminder(ti):
        """Set a reminder."""
        text = ti.get("text", "").strip()
        due = ti.get("due", "").strip()
        recurrence = ti.get("recurrence") or "none"
        condition = ti.get("condition") or None

        if not text:
            return "Error: reminder text is required."
        if not due:
            return "Error: due datetime is required."

        try:
            # Checked first so a bad recurrence is reported as one: BAD_DUE_TS
            # includes ValueError, so the handler below would blame the timestamp.
            parse_recurrence(recurrence)
        except ValueError as exc:
            return f"Error: {exc}"

        try:
            # Echo the NORMALIZED time, not the raw input: add() rewrites an
            # offset-aware due to local naive, so echoing `due` would confirm a
            # different time than the one that actually fires.
            due = normalize_due_ts(due)
            reminder_id = reminder_store.add(text, due, recurrence, condition=condition)
            return f"Reminder #{reminder_id} set for {due}"
        except BAD_DUE_TS as exc:
            # The recurrence was validated above, so what is left is the timestamp.
            return f"Error: due is not a valid ISO datetime: {due!r} ({exc})"

    def list_reminders(ti):
        """List active reminders."""
        reminders = reminder_store.list_active()

        if not reminders:
            return "No active reminders."

        now = clock()
        lines = []
        # Show the real DB id (what cancel_reminder takes), never the row
        # position -- after any cancellation they diverge and "cancel reminder
        # 1" would hit the wrong reminder.
        for reminder in reminders:
            text = reminder["text"]
            due = speak_due(reminder["due_ts"], now)
            recurrence = reminder["recurrence"]
            recurrence_str = "" if recurrence == "none" else f" (repeats {speak_recurrence(recurrence)})"
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
        "timer_status": timer_status,
        "timer_control": timer_control,
        "set_reminder": set_reminder,
        "list_reminders": list_reminders,
        "cancel_reminder": cancel_reminder,
    }
