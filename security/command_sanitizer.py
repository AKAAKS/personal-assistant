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
    for pattern, reason in _BLOCKLIST:
        if pattern.search(stripped):
            return False, f"blocked: this command {reason}"
    return True, ""
