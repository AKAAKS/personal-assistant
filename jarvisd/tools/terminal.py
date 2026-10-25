"""Terminal & PC-power tools (Tier A + B PC control).

- open_terminal   (Tier A, write)        open a visible terminal, optionally
                                          running a command the user then drives.
- run_command     (Tier B, destructive)  run a command headlessly, return output.
- scan_unsaved_work (read_only)           best-effort list of open apps that may
                                          hold unsaved work (for the power flow).
- system_power    (destructive)           restart / shutdown (without force, so
                                          apps prompt to save) / cancel.

Headless shell (run_command) and interactive-terminal launching (open_terminal)
are both gated by the JARVIS_ALLOW_SHELL kill switch (default on) and run every
command through security.command_sanitizer first. run_command and system_power
are destructive-tier, so the security guard also requires user confirmation.
"""

import os

from security.command_sanitizer import sanitize_command

OUTPUT_TAIL_CHARS = 2000

# Window-title signals that an app may hold unsaved work. Known editors/office
# apps (substring match on the window title) + leading "dirty" markers.
_AT_RISK_APPS = (
    "Notepad", "Notepad++", "Visual Studio Code", "Visual Studio",
    "Word", "Excel", "PowerPoint", "Sublime Text", "Vim", "Emacs",
    "Photoshop", "GIMP", "Illustrator", "Figma", "Obsidian",
)
_DIRTY_MARKERS = ("*", "●", "•")  # *,  ●, •  — common "modified" glyphs

TOOLS = [
    {
        "name": "open_terminal",
        "description": (
            "Open a visible terminal window on the PC, optionally running a command "
            "and keeping the window open so the user can interact with it (e.g. run "
            "the 'claude' CLI, or start a dev server). Use this when the user wants a "
            "terminal they'll type into themselves."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Optional command to run in the new terminal, e.g. 'claude' or 'npm run dev'.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "run_command",
        "description": (
            "Run a command on the PC and return its output (destructive — requires "
            "confirmation). Use for one-off commands where the user wants the result "
            "back (e.g. 'git status', 'ipconfig'). For something the user will interact "
            "with, use open_terminal instead."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The command line to run."},
            },
            "required": ["command"],
        },
    },
    {
        "name": "scan_unsaved_work",
        "description": (
            "Check which open applications may have unsaved work. Call this BEFORE a "
            "shutdown or restart so you can warn the user. Best-effort (Windows has no "
            "reliable unsaved-work signal) — reports likely editors/documents that are open."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "system_power",
        "description": (
            "Restart or shut down the PC, or cancel a pending shutdown (destructive — "
            "requires confirmation). Shutdown/restart is issued WITHOUT force, so each "
            "app shows its own save prompt. Call scan_unsaved_work first and warn the user."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["restart", "shutdown", "cancel"],
                    "description": "What to do.",
                },
                "grace_seconds": {
                    "type": "integer",
                    "description": "Seconds before it happens (default 60), giving a window to cancel.",
                },
            },
            "required": ["action"],
        },
    },
]


def _shell_enabled() -> bool:
    return os.getenv("JARVIS_ALLOW_SHELL", "1").strip().lower() not in ("0", "false", "no", "off")


def handlers(system):
    """Terminal/power tool handlers closing over a SystemClient."""

    def open_terminal(ti):
        if not _shell_enabled():
            return "Terminal/shell access is disabled (JARVIS_ALLOW_SHELL is off)."
        command = ti.get("command")
        if command is not None:
            if not isinstance(command, str) or not command.strip():
                return "Error: command must be a non-empty string (or omit it for an empty terminal)."
            command = command.strip()
            ok, reason = sanitize_command(command)
            if not ok:
                return f"Won't open a terminal for that — {reason}."
        try:
            system.open_terminal(command)
        except Exception as e:
            return f"Error opening terminal: {e}"
        return f"Opened a terminal running '{command}'." if command else "Opened a terminal."

    def run_command(ti):
        if not _shell_enabled():
            return "Running commands is disabled (JARVIS_ALLOW_SHELL is off)."
        command = ti.get("command")
        if not isinstance(command, str) or not command.strip():
            return "Error: command is required and must be a non-empty string."
        command = command.strip()
        ok, reason = sanitize_command(command)
        if not ok:
            return f"Refused — {reason}."
        try:
            code, out, err = system.run_command(command)
        except Exception as e:
            return f"Error running command: {e}"
        return (
            f"Exit code: {code}\n"
            f"--- stdout (last {OUTPUT_TAIL_CHARS} chars) ---\n{(out or '')[-OUTPUT_TAIL_CHARS:]}\n"
            f"--- stderr (last {OUTPUT_TAIL_CHARS} chars) ---\n{(err or '')[-OUTPUT_TAIL_CHARS:]}"
        )

    def scan_unsaved_work(ti):
        try:
            windows = system.list_windows()
        except Exception as e:
            return f"Error scanning windows: {e}"
        flagged = []
        for _pid, title in windows:
            t = title.strip()
            dirty = t[:1] in _DIRTY_MARKERS
            known = any(app.lower() in t.lower() for app in _AT_RISK_APPS)
            if dirty or known:
                flagged.append(t)
        if not flagged:
            return "No open apps obviously have unsaved work (best-effort — can't be certain)."
        listed = "; ".join(flagged)
        return (
            f"These open apps may have unsaved work: {listed}. "
            "This is best-effort and may miss some — proceed only if the user confirms."
        )

    def system_power(ti):
        action = ti.get("action")
        if action not in ("restart", "shutdown", "cancel"):
            return "Error: action must be 'restart', 'shutdown', or 'cancel'."
        grace = ti.get("grace_seconds")
        if not isinstance(grace, int) or grace < 0:
            grace = 60
        try:
            return system.power(action, grace_seconds=grace)
        except Exception as e:
            return f"Error issuing power command: {e}"

    return {
        "open_terminal": open_terminal,
        "run_command": run_command,
        "scan_unsaved_work": scan_unsaved_work,
        "system_power": system_power,
    }
