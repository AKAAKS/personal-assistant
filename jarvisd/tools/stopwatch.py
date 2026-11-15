"""Stopwatch tool: start, pause, resume, stop and check a count-up stopwatch."""

from datetime import datetime

from jarvisd.reminder_store import BAD_DUE_TS
from jarvisd.stopwatch_store import elapsed_sec
from jarvisd.text_format import speak_duration

_ACTIONS = ("start", "pause", "resume", "stop", "check")

TOOLS = [
    {
        "name": "stopwatch_control",
        "description": (
            "Run a STOPWATCH, which counts UP from zero to measure how long something takes "
            "('start a stopwatch', 'time how long this takes', 'how long has it been', "
            "'pause / resume / stop the stopwatch'). It is NOT a countdown: for 'tell me in "
            "20 minutes' or 'set a 10-minute timer' use set_timer instead. Actions: start, "
            "pause, resume, stop (reports the final time and ends it), check (reports the "
            "elapsed time without stopping). Optional label names it ('run'); with several "
            "stopwatches, name the one you mean by label, otherwise the only one is used."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": list(_ACTIONS),
                    "description": "What to do with the stopwatch.",
                },
                "label": {
                    "type": "string",
                    "description": "Optional name, e.g. 'run'. Names a new stopwatch on start; picks one on other actions.",
                },
            },
            "required": ["action"],
        },
    },
]


def _name(row) -> str:
    return f"The {row['label']} stopwatch" if row["label"] else "The stopwatch"


def handlers(stopwatch_store, clock=datetime.now):
    """Stopwatch tool handlers: tool name -> callable(tool_input: dict) -> str."""

    def _line(row, now):
        try:
            secs = elapsed_sec(row, now)
        except BAD_DUE_TS:
            return f"{_name(row)} has an unreadable time."
        state = "paused at" if row["status"] == "paused" else "at"
        return f"{_name(row)} is {state} {speak_duration(secs)}."

    def _pick(label, now, verb):
        """Return (row, None) or (None, message) for the stopwatch an action targets."""
        rows = stopwatch_store.active(now)
        if not rows:
            return None, f"There is no stopwatch to {verb}."
        if label:
            wanted = label.casefold()
            rows = [r for r in rows if r["label"].casefold() == wanted]
            if not rows:
                return None, f"There is no stopwatch called {label}."
        if len(rows) > 1:
            names = ", ".join(r["label"] or "unnamed" for r in rows)
            return None, f"Which stopwatch do you mean? You have: {names}."
        return rows[0], None

    def stopwatch_control(ti):
        action = ti.get("action")
        if action not in _ACTIONS:
            return f"Error: unknown stopwatch action {action!r}; use one of {', '.join(_ACTIONS)}."
        raw = ti.get("label")  # models can send an explicit null
        label = raw.strip() if isinstance(raw, str) else ""
        now = clock()

        if action == "start":
            stopwatch_store.start(label, now=now)
            return f"Started the {label} stopwatch." if label else "Started the stopwatch."

        row, msg = _pick(label, now, "check" if action == "check" else action)
        if action == "check" and row is None and msg.startswith("Which"):
            lines = [_line(r, now) for r in stopwatch_store.active(now)
                     if not label or r["label"].casefold() == label.casefold()]
            return " ".join(lines)
        if row is None:
            return msg

        if action == "check":
            return _line(row, now)
        if action == "pause":
            if row["status"] == "paused":
                return f"{_name(row)} is already paused."
            if stopwatch_store.pause(row["id"], now):
                return f"Paused. {_line(stopwatch_store.get(row['id']), now)}"
            return f"{_name(row)} could not be paused."
        if action == "resume":
            if row["status"] == "running":
                return f"{_name(row)} is already running."
            if stopwatch_store.resume(row["id"], now):
                return f"Resumed {_name(row).lower()}."
            return f"{_name(row)} could not be resumed."
        total = stopwatch_store.stop(row["id"], now)  # stop
        if total is None:
            return f"{_name(row)} could not be stopped."
        return f"Stopped. {_name(row)} ran for {speak_duration(total)}."

    return {"stopwatch_control": stopwatch_control}
