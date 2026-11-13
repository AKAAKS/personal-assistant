"""`claude_status` — what the Claude Code sessions on this PC are doing.

The user starts a long Claude Code turn and walks away. Half the time the
session is not working at all: it finished two minutes ago, or it is sitting on
a permission prompt waiting for one keystroke. This tool answers "is Claude
still going?" from the records the status hook leaves in
`data/claude_sessions/`, with no screenshot, no vision and no extra API call.

Read-only in every sense: it reads the state directory and never writes to it.
The hook is that directory's only writer (see `jarvisd.claude_sessions`).

Three answers have to stay distinct, because a confident wrong answer sends the
user back to the PC for nothing:

  - the state directory is MISSING -> the hooks were never installed. Saying
    "nothing is running" here would be the wrong answer, not a vaguer one.
  - the directory is present and empty -> nothing is running.
  - live records -> one clause each.

A session that finished more than DONE_REPORT_SEC ago is counted rather than
quoted. Otherwise someone with four terminals open all day gets four stale
snippets on every check, and four stale snippets transit to the API each time.
"""

import re
import time

from jarvisd import claude_sessions as cs
from jarvisd.text_format import strip_markdown

# A finished session older than this is counted, not quoted: 30 minutes.
DONE_REPORT_SEC = 1800

TOOLS = [
    {
        "name": "claude_status",
        "description": (
            "Check what the Claude Code coding sessions running on the user's PC are "
            "doing: working, waiting on a permission prompt, or finished (and what the "
            "last one said). Use this for questions like 'is Claude still going?', "
            "'has Claude finished?', 'is it waiting on me?'. This is about the Claude "
            "Code CLI sessions on the PC — NOT about you, your own thinking, or any "
            "other app."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
]


def _duration(seconds: float) -> str:
    """A spoken-friendly age: "4 minutes", "2 hours 10 minutes"."""
    seconds = max(0.0, float(seconds))
    if seconds < 60:
        return "less than a minute"
    minutes = int(seconds // 60)
    if minutes < 60:
        return "1 minute" if minutes == 1 else f"{minutes} minutes"
    hours, minutes = divmod(minutes, 60)
    hour_text = "1 hour" if hours == 1 else f"{hours} hours"
    if minutes == 0:
        return hour_text
    return f"{hour_text} {minutes} minutes"


def _spoken(message: str) -> str:
    """A Claude reply as one line of plain prose.

    The result is read aloud by the voice client, so markdown has to go and
    every newline has to collapse -- a bulleted list spoken verbatim is noise.
    """
    return re.sub(r"\s+", " ", strip_markdown(str(message))).strip()


def _labels(records) -> list[str]:
    """A name per record: the project, numbered when a project repeats.

    Two sessions in one project is the normal case for this user (one terminal
    running tests, one editing), so they must be told apart. The numbers follow
    the report order, which is most-recent-state-change first.
    """
    counts: dict[str, int] = {}
    for record in records:
        project = record.get("project") or "an unknown project"
        counts[project] = counts.get(project, 0) + 1
    seen: dict[str, int] = {}
    labels = []
    for record in records:
        project = record.get("project") or "an unknown project"
        if counts[project] == 1:
            labels.append(project)
        else:
            seen[project] = seen.get(project, 0) + 1
            labels.append(f"{project} (session {seen[project]})")
    return labels


def _clause(record: dict, label: str, age: float) -> str:
    state = record.get("state")
    duration = _duration(age)
    if state == cs.WAITING:
        tool = record.get("tool_name")
        target = f" to use {tool}" if tool else ""
        return (f"Claude needs your input in {label}{target}, "
                f"and has been waiting {duration}.")
    if state == cs.DONE:
        message = record.get("last_message")
        if message:
            return (f"Claude finished in {label} {duration} ago and said: "
                    f"{_spoken(message)}")
        return f"Claude finished in {label} {duration} ago."
    if state == cs.IDLE:
        # Neither working nor finished: a terminal nobody has typed into. It
        # gets its own clause rather than being folded into either, because
        # both would be untrue.
        return f"Claude is open in {label} with nothing running, {duration} now."
    return f"Claude is working in {label}, {duration} so far."


def summarize(records, *, now) -> str:
    """One spoken paragraph describing `records`. No newlines in the result."""
    if not records:
        return "No Claude Code sessions are running."

    reference = now()
    quoted, stale = [], 0
    for record in records:
        age = reference - (record.get("since") or reference)
        if record.get("state") == cs.DONE and age > DONE_REPORT_SEC:
            stale += 1
        else:
            quoted.append((record, age))

    parts = [_clause(record, label, age)
             for (record, age), label in
             zip(quoted, _labels([r for r, _ in quoted]))]

    if stale:
        parts.append(
            f"{stale} other session{'s' if stale > 1 else ''} finished "
            "more than half an hour ago."
            if quoted else
            f"{stale} session{'s' if stale > 1 else ''} finished "
            "more than half an hour ago, nothing since."
        )

    return re.sub(r"\s+", " ", " ".join(parts)).strip()


def handlers(*, directory=None, now=time.time, probe=cs._psutil_probe):
    """The claude_status handler. `probe` is injected only by the tests."""
    state_dir = cs.DIR if directory is None else directory

    def claude_status(ti):
        try:
            if not cs.Path(state_dir).is_dir():
                return ("The Claude Code status hooks aren't installed yet, so I "
                        "can't see any sessions. Run scripts/install_claude_hooks.py "
                        "to set them up.")
            records = cs.read_sessions(directory=state_dir, now=now, probe=probe)
            return summarize(records, now=now)
        except Exception as e:
            return f"Error reading Claude Code status: {e}"

    return {"claude_status": claude_status}
