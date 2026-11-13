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

try:  # psutil is a dependency, but liveness must degrade rather than crash
    import psutil
except Exception:  # pragma: no cover - exercised only on a broken install
    psutil = None

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

# Only for a record that could not find its claude.exe ancestor: 12 hours, so a
# weird launch shape degrades to "eventually forgotten" instead of either
# "immediately invisible" or "busy forever".
NO_PID_TTL_SEC = 43200

# A probe result meaning "the process is running but we cannot read it"
# (psutil.AccessDenied). It must count as LIVE: a session we cannot inspect is
# still a session, and calling it dead is the confident wrong answer the design
# forbids.
START_UNKNOWN = -1.0

_CLAUDE_PROCESS = "claude.exe"

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


def _psutil_walker(start_pid=None):
    """Yield (pid, name, create_time) for this process and each ancestor."""
    if psutil is None:
        return
    proc = psutil.Process(start_pid) if start_pid else psutil.Process()
    seen = 0
    while proc is not None and seen < 32:  # a cycle would otherwise hang a hook
        seen += 1
        try:
            yield proc.pid, proc.name(), proc.create_time()
        except Exception:
            return
        try:
            proc = proc.parent()
        except Exception:
            return


def claude_ancestor(*, start_pid=None, walker=_psutil_walker):
    """The nearest `claude.exe` ancestor as (pid, start_time), or None.

    `os.getppid()` is useless here: the verified chain from a hook-like child
    on this machine is `bash -> bash -> bash -> claude.exe -> powershell ->
    WindowsTerminal`, so the hook's own parent is a transient shell. The start
    time comes back with the pid because the pid alone is not an identity --
    Windows reuses pids, and a reused one would keep a dead session "busy".
    """
    try:
        for entry in walker(start_pid):
            pid, name = entry[0], entry[1]
            started = entry[2] if len(entry) > 2 else START_UNKNOWN
            if str(name or "").lower() == _CLAUDE_PROCESS:
                return pid, started
    except Exception as e:
        log.debug("claude_sessions: ancestor walk failed: %s", e)
    return None


def _psutil_probe(pid):
    """The start time of `pid`: None if it is gone, START_UNKNOWN if opaque."""
    if psutil is None:
        return START_UNKNOWN
    try:
        return psutil.Process(pid).create_time()
    except psutil.NoSuchProcess:
        return None
    except Exception:
        # AccessDenied and anything else: it exists as far as we can tell.
        return START_UNKNOWN


def is_live(record, *, now=time.time, probe=_psutil_probe) -> bool:
    """Is the Claude session this record describes still running?

    Liveness, not a timer: a session blocked on a permission prompt for three
    hours is in a state that is still true, and a TTL would quietly delete the
    most useful answer this feature has.
    """
    try:
        pid = record.get("claude_pid")
        if not pid:
            updated = record.get("updated_ts")
            if not isinstance(updated, (int, float)):
                return False
            return (now() - updated) <= NO_PID_TTL_SEC
        started = probe(pid)
        if started is None:
            return False  # the process is gone
        if started == START_UNKNOWN:
            return True   # running, just not inspectable by us
        recorded = record.get("claude_started")
        if not isinstance(recorded, (int, float)) or recorded == START_UNKNOWN:
            return True   # nothing to compare against; trust the live pid
        return abs(started - recorded) < 1.0
    except Exception as e:
        log.debug("claude_sessions: liveness check failed: %s", e)
        return False


def read_sessions(*, directory=DIR, now=time.time, probe=_psutil_probe) -> list[dict]:
    """The live records, most recent state change first."""
    live = [r for r in read_raw(directory=directory)
            if is_live(r, now=now, probe=probe)]
    live.sort(key=lambda r: r.get("since") or 0, reverse=True)
    return live


def apply_event(payload: dict, *, directory=DIR, now=time.time,
                ancestor=claude_ancestor) -> None:
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

        # Re-resolved on every write: the hook is a fresh child of the same
        # claude.exe each time, and a record that loses its pid would start
        # expiring on the 12-hour fallback instead of on liveness.
        try:
            found = ancestor()
        except Exception as e:
            log.debug("claude_sessions: ancestor lookup failed: %s", e)
            found = None
        record["claude_pid"] = found[0] if found else None
        record["claude_started"] = found[1] if found else None

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
