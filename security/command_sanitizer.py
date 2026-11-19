"""Denylist sanitizer for arbitrary shell commands (Tier B PC control).

Design note: this is intentionally a DENYLIST, not the allowlist-first model of
OS_agent's agentkernel sanitizer. JARVIS's Tier-B goal is to run *arbitrary* dev
commands the user asks for, so a hard executable allowlist would defeat the
feature. The real controls are the mandatory per-command confirmation (the
human approves every command — run_command is a destructive-tier tool) and the
JARVIS_ALLOW_SHELL kill switch. This denylist is defense-in-depth on top: it
blocks the catastrophic, irreversible one-shots (disk format/wipe, recursive
mass-delete, registry deletion) and download-and-execute cradles even if a
command is reflexively confirmed or arrives via prompt injection.

Ordinary pipes and redirects are allowed (too useful to block, and the human
confirms); only piping a download straight into a shell/interpreter is blocked.
"""

import re

# Shell and core system processes. Killing any of these blanks the desktop and
# taskbar (explorer.exe) or destabilises the OS. system_client refuses them in
# close_app/force_close_app; this blocks the same action arriving as a raw
# shell command, which is the door the tool guard does not cover.
_SHELL_PROCS = (
    r"(?:explorer|winlogon|csrss|wininit|smss|services|lsass|dwm|svchost"
    r"|sihost|ctfmon|taskhostw|fontdrvhost)"
)
_KILLS_SHELL = (
    "kills a Windows shell/system process — it would blank the desktop and "
    "taskbar; close the app by name instead"
)
_WILDCARD_KILL = (
    "kills processes by a wildcard image name, which can match a Windows shell "
    "process and blank the desktop; name the exact process instead"
)

# A construct that actually terminates a process. `\bkill\b` does not match
# inside "taskkill", nor does it match "killing" in a commit message.
_KILL_VERB_RE = re.compile(
    r"\b(taskkill|stop-process|spps)\b|\bkill\b|\bwmic\b[^|&;]*\bdelete\b", re.I
)

# A protected name in a TARGET position. Matching the name anywhere would flag
# `taskkill /IM chrome.exe /FI "WINDOWTITLE ne explorer"`, which kills Chrome.
_KILL_TARGET_RES = [
    re.compile(rf"/im\s+[\"\']?{_SHELL_PROCS}", re.I),
    re.compile(rf"-name\s+[\"\']?{_SHELL_PROCS}", re.I),
    re.compile(rf"\b(?:get-process|gps|ps)\s+(?:-name\s+)?[\"\']?{_SHELL_PROCS}", re.I),
    re.compile(rf"\bname\s*=\s*[\"\']?{_SHELL_PROCS}", re.I),
]

# A wildcard image name: unknowable blast radius (`taskkill /IM *.exe /F`), and
# how `taskkill /IM expl*.exe /F` reached explorer.exe past an exact-name list.
_WILDCARD_IMAGE_RE = re.compile(r"/im\s+[\"\']?[^\s\"\']*[*?]", re.I)


def _kills_shell_process(command: str) -> "str | None":
    """Reason this command terminates a shell/system process, else None.

    Verb AND target, not one regex over the whole string: the kill verb and its
    target are routinely in different pipeline segments
    (`Get-Process explorer | Stop-Process -Force`), while a protected name can
    appear in a filter that targets something else entirely.
    """
    if not _KILL_VERB_RE.search(command):
        return None
    if _WILDCARD_IMAGE_RE.search(command):
        return _WILDCARD_KILL
    if any(r.search(command) for r in _KILL_TARGET_RES):
        return _KILLS_SHELL
    return None


# (compiled pattern, human-readable reason). Case-insensitive.
_BLOCKLIST: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bformat\s+[a-z]:", re.I), "formats a disk"),
    (re.compile(r"\bdiskpart\b", re.I), "runs diskpart (disk partitioning)"),
    (re.compile(r"\bcipher\s+/w", re.I), "wipes free disk space"),
    (re.compile(r"\bdel\s+/[sfq]", re.I), "mass-deletes files (del /s /f /q)"),
    (re.compile(r"\b(rmdir|rd)\s+/s", re.I), "recursively removes a directory tree"),
    (re.compile(r"\brm\s+-[a-z]*[rf]", re.I), "recursively/forcibly removes files (rm -r/-f)"),
    (re.compile(r"remove-item\b.*-recurse", re.I), "recursively removes items (Remove-Item -Recurse)"),
    (re.compile(r"\breg\s+delete\b", re.I), "deletes registry keys"),
    (re.compile(r"remove-item\s+hk(lm|cu)", re.I), "deletes registry keys"),
    (re.compile(r"\bshutdown\b", re.I), "shuts down/restarts the PC — use the power control tool instead"),
    (re.compile(r"\b(restart|stop)-computer\b", re.I), "shuts down/restarts the PC — use the power control tool instead"),
    (re.compile(r"\blogoff\b", re.I), "logs the user off"),
    # Download-and-execute cradles.
    (re.compile(r"invoke-expression", re.I), "executes a downloaded/dynamic string (Invoke-Expression)"),
    (re.compile(r"\biex\b", re.I), "executes a downloaded/dynamic string (IEX)"),
    (re.compile(r"downloadstring", re.I), "downloads and runs code (DownloadString)"),
    (re.compile(r"downloadfile", re.I), "downloads a file to execute (DownloadFile)"),
    (re.compile(r"\|\s*(iex|bash|sh|cmd|powershell|pwsh)\b", re.I), "pipes a download straight into a shell"),
]


def sanitize_command(command: str) -> tuple[bool, str]:
    """Validate a command string.

    Returns (allowed, reason). allowed=True -> safe to run (reason is ""),
    allowed=False -> blocked (reason explains why, for a friendly message).
    """
    if not command or not command.strip():
        return False, "empty command"
    stripped = command.strip()
    shell_kill = _kills_shell_process(stripped)
    if shell_kill:
        return False, f"blocked: this command {shell_kill}"
    for pattern, reason in _BLOCKLIST:
        if pattern.search(stripped):
            return False, f"blocked: this command {reason}"
    return True, ""
