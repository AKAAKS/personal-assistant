"""What each Claude Code session on this PC is doing, as one small file each.

This module exists because three processes have to agree on one record shape
and none of them can call the others. A Claude Code hook runs as a short-lived
child of `claude.exe`, in whatever directory the coding session happens to be
in, and writes; the daemon and the `claude_status` tool read. A file is the
whole mechanism, exactly as `clients/activity.py` publishes voice state to the
tray.

So this file is the SINGLE OWNER of the schema: field names, the event-to-state
routing, and the atomic write live here and nowhere else. The hook script is a
twelve-line adapter over `apply_event`; the daemon and the tool only read.

**The hook is the only writer.** The daemon never writes here. A cross-process
read-modify-write racing the hook's `os.replace` could revert a state the hook
had just recorded, and a status feature that reports a state which was never
true is worse than one that reports nothing. Announcement dedupe therefore
lives in daemon memory.

Two fields have independent lifetimes on purpose:

  - `state` is what the session is doing now. `since` moves only when `state`
    changes value, so "how long has it been like this" stays true across the
    repeated events of one state.
  - `last_message` is written only by `Stop` and cleared only by the next
    `UserPromptSubmit`. An earlier draft kept a single state field, and an idle
    notification 60s after `Stop` both flipped a finished session to "waiting
    on you" and destroyed the message it had just recorded. `idle_ts` is an
    event *about* the current state and never changes `state`.

Nothing here raises. A failed status write must never take a Claude Code turn
down with it -- the coding session is the work, this is a nicety.
"""

import json
import logging
import os
import tempfile
import time
from pathlib import Path

log = logging.getLogger(__name__)

# --- states ----------------------------------------------------------------
WORKING = "working"   # a turn is in flight
WAITING = "waiting"   # blocked on the user: a permission prompt
DONE = "done"         # the turn finished; the session sits at an empty prompt

# Derived from __file__, never from the cwd: the hook inherits the Claude
# session's working directory, which is a different project every time.
DIR = (Path(__file__).resolve().parent.parent / "data" / "claude_sessions")

# Bounds the record, and bounds what transits to the API when the user asks
# and the message comes back as tool output.
MESSAGE_CAP = 500

# A Notification whose text contains one of these is the permission prompt
# rather than the idle nudge. The payload shape for Notification is not
# published, so this reads whatever text fields are present and falls back to
# "idle" -- the state that announces the least.
_PERMISSION_HINTS = ("permission", "needs your approval", "approve")

_SAFE = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")


def _record_path(directory, session_id: str) -> Path | None:
    """The file for a session, or None if the id cannot safely name one.

    A session id arrives from outside this process, so it is filtered down to
    a known-safe alphabet rather than trusted to be a bare uuid.
    """
    safe = "".join(c for c in str(session_id) if c in _SAFE)
    if not safe:
        return None
    return Path(directory) / f"{safe}.json"


def _load(path: Path) -> dict:
    """The record at `path`, or an empty one. Corrupt reads as absent."""
    try:
        with open(path, encoding="utf-8") as f:
            record = json.load(f)
        return record if isinstance(record, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        log.debug("claude_sessions: unreadable record %s: %s", path, e)
        return {}


def _write(path: Path, record: dict) -> None:
    """Replace `path` with `record` atomically.

    Same mechanism as clients/activity.py: a sibling temp file, then a rename
    over the target, so a reader sees either the whole old record or the whole
    new one and never a half-written state.
    """
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(directory), prefix=".claude-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(record, f)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _project(cwd) -> str | None:
    """The project name a path belongs to: its last component.

    Split on both separators because the hook may report a POSIX-style path
    from a bash-launched session and a Windows one otherwise.
    """
    if not cwd:
        return None
    name = str(cwd).replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    return name or None


def _looks_like_permission(payload: dict) -> bool:
    text = " ".join(
        str(payload.get(key) or "")
        for key in ("message", "matcher", "notification_type", "title")
    ).lower()
    if "permission_prompt" in text:
        return True
    return any(hint in text for hint in _PERMISSION_HINTS)


def apply_event(payload: dict, *, directory=DIR, now=time.time) -> None:
    """Fold one hook event into that session's record. Never raises."""
    try:
        if not isinstance(payload, dict):
            return
        event = payload.get("hook_event_name")
        session_id = payload.get("session_id")
        path = _record_path(directory, session_id) if session_id else None
        if path is None:
            return

        if event == "SessionEnd":
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            return

        record = _load(path)
        ts = now()
        state = record.get("state")
        new_state = state
        idle_ts = record.get("idle_ts")

        if event in ("SessionStart", "UserPromptSubmit"):
            new_state = WORKING
            record["last_message"] = None
        elif event == "PermissionRequest":
            new_state = WAITING
            record["tool_name"] = payload.get("tool_name")
        elif event == "Notification":
            if _looks_like_permission(payload):
                new_state = WAITING
            else:
                # Idle is an event about the current state, not a state.
                idle_ts = ts
        elif event == "Stop":
            new_state = DONE
            message = payload.get("last_assistant_message")
            record["last_message"] = (
                str(message)[:MESSAGE_CAP] if message is not None else None
            )
        else:
            return  # an event we never registered, or one newer than this code

        record["session_id"] = session_id
        record["state"] = new_state
        # `since` is the age of the CURRENT state, so it survives repeat events.
        record["since"] = ts if new_state != state else record.get("since", ts)
        record["updated_ts"] = ts
        record["idle_ts"] = idle_ts
        cwd = payload.get("cwd")
        if cwd:
            record["cwd"] = cwd
            record["project"] = _project(cwd)
        record.setdefault("cwd", None)
        record.setdefault("project", None)
        record.setdefault("tool_name", None)
        record.setdefault("last_message", None)
        mode = payload.get("permission_mode")
        if mode:
            record["permission_mode"] = mode
        record.setdefault("permission_mode", None)

        _write(path, record)
    except Exception as e:
        log.debug("claude_sessions: apply_event failed: %s", e)


def read_raw(*, directory=DIR) -> list[dict]:
    """Every record on disk, corrupt ones skipped. No liveness filtering."""
    records = []
    try:
        paths = sorted(Path(directory).glob("*.json"))
    except Exception as e:
        log.debug("claude_sessions: cannot list %s: %s", directory, e)
        return records
    for path in paths:
        record = _load(path)
        if record.get("state"):
            records.append(record)
    return records
