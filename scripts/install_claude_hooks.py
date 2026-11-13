"""Register the Claude Code status hooks in ~/.claude/settings.json.

Run it once:

    python scripts/install_claude_hooks.py

That file is the user's own Claude Code configuration -- `permissions`,
`model`, `mcpServers`, `enabledPlugins` -- and it lives outside this repo, so
this script is written to be the opposite of destructive:

  - **Merge, never overwrite.** Unrelated keys are untouched, and so is any
    hook someone else registered, including on the six events we also use.
  - **Back up first**, to a timestamped sibling, before writing anything.
  - **Idempotent.** Our entries are recognised by the script path in their
    command, so a second run reports "already installed" and changes nothing.
  - **Refuse rather than clobber.** Settings that will not parse, or that are
    not a JSON object, raise instead of being replaced with a fresh file.

Every entry is `async: true`: a synchronous hook would let a status write stall
a Claude Code turn, and the coding session is the work.
"""

import json
import sys
import time
from pathlib import Path

# The six lifecycle events. No single one is load-bearing: with only
# UserPromptSubmit and Stop the feature still answers working/finished/nothing,
# and the payload shapes for Notification and PermissionRequest are not
# published, so whatever does not fire simply never appears.
EVENTS = (
    "SessionStart",       # a session opened -> working
    "UserPromptSubmit",   # a turn started -> working, clear the last message
    "PermissionRequest",  # blocked on a tool approval -> waiting
    "Notification",       # permission prompt -> waiting; idle -> announceable
    "Stop",               # the turn finished -> done, record what it said
    "SessionEnd",         # the session closed -> forget it
)

_MARKER = "claude_status_hook.py"


def hook_entries(script_path: str, *, python_exe: str | None = None) -> dict:
    """The `hooks` fragment registering `script_path` for all six events.

    The path is resolved to an absolute one because Claude Code runs a hook
    with the coding session's working directory, which is a different project
    every time.
    """
    script = Path(script_path).expanduser().resolve()
    exe = python_exe or sys.executable or "python"
    command = f'"{exe}" "{script}"'
    return {
        event: [{"hooks": [{"type": "command", "command": command,
                            "async": True, "timeout": 10}]}]
        for event in EVENTS
    }


def _load(settings_path: Path) -> dict:
    if not settings_path.exists():
        return {}
    text = settings_path.read_text(encoding="utf-8")
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(
            f"{settings_path} is not valid JSON ({e}); refusing to touch it. "
            "Fix or move the file and run this again."
        ) from e
    if not isinstance(data, dict):
        raise ValueError(
            f"{settings_path} does not contain a JSON object; refusing to touch it."
        )
    return data


def _already_registered(entries) -> bool:
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        for hook in entry.get("hooks", []):
            if isinstance(hook, dict) and _MARKER in str(hook.get("command", "")):
                return True
    return False


def install(settings_path, *, script_path, backup=True) -> str:
    """Merge the status hooks into `settings_path`. Returns a summary line."""
    settings_path = Path(settings_path)
    settings = _load(settings_path)

    wanted = hook_entries(script_path)
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}

    added = []
    for event, entries in wanted.items():
        existing = hooks.get(event)
        if not isinstance(existing, list):
            existing = [] if existing is None else [existing]
        if _already_registered(existing):
            continue
        hooks[event] = existing + entries
        added.append(event)

    if not added:
        return (f"The Claude Code status hooks are already installed in "
                f"{settings_path} for: {', '.join(EVENTS)}. Nothing changed.")

    if backup and settings_path.exists():
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup_path = settings_path.with_name(settings_path.name + f".bak.{stamp}")
        backup_path.write_text(settings_path.read_text(encoding="utf-8"),
                               encoding="utf-8")
        backup_note = f" Backed up the previous file to {backup_path.name}."
    else:
        backup_note = ""

    settings["hooks"] = hooks
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")

    return (f"Registered the Claude Code status hook in {settings_path} for: "
            f"{', '.join(added)}.{backup_note} "
            f"Events already present were left alone; the full set is "
            f"{', '.join(EVENTS)}.")


if __name__ == "__main__":
    hook = Path(__file__).resolve().parent / "claude_status_hook.py"
    target = Path.home() / ".claude" / "settings.json"
    print(install(target, script_path=str(hook)))
