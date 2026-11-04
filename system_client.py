"""Local system control — launch/close apps, system info, clipboard."""

import os
import shlex
import subprocess

# How long a headless run_command may run before it is killed.
COMMAND_TIMEOUT_SEC = 60

# Anthropic credentials stripped from any child process JARVIS spawns to run
# Claude Code, so Claude Code uses the user's subscription login instead of
# JARVIS's API key. JARVIS's own brain uses the key IN-PROCESS (never via a
# spawned child), so scrubbing a child's env never affects the assistant itself.
_ANTHROPIC_CRED_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


def _scrubbed_env() -> dict:
    """A copy of the current environment with JARVIS's Anthropic creds removed."""
    env = os.environ.copy()
    for var in _ANTHROPIC_CRED_VARS:
        env.pop(var, None)
    return env


def _launches_claude(command: "str | None") -> bool:
    """True when `command`'s program is the Claude Code CLI (`claude`).

    Only these spawns get Anthropic credentials scrubbed; an empty terminal or
    any other command keeps JARVIS's inherited environment. Matches `claude`,
    `claude --resume`, and a full/quoted path ending in `claude[.exe|.cmd|...]`,
    but not an unrelated program that merely starts with "claude".
    """
    if not command or not command.strip():
        return False
    try:
        tokens = shlex.split(command, posix=False)
    except ValueError:
        tokens = command.split()
    if not tokens:
        return False
    prog = os.path.basename(tokens[0].strip().strip('"').strip("'")).lower()
    return prog == "claude" or prog.startswith("claude.")


class SystemClient:

    def close_app(self, name: str) -> str:
        name_lower = name.lower().strip()
        process_map = {
            "firefox": "firefox.exe",
            "chrome": "chrome.exe",
            "edge": "msedge.exe",
            "notepad": "notepad.exe",
            "explorer": "explorer.exe",
            "spotify": "Spotify.exe",
            "discord": "Discord.exe",
            "steam": "steam.exe",
            "vscode": "Code.exe",
            "vs code": "Code.exe",
            "word": "WINWORD.EXE",
            "excel": "EXCEL.EXE",
            "powerpoint": "POWERPNT.EXE",
            "teams": "ms-teams.exe",
            "slack": "slack.exe",
            "obs": "obs64.exe",
            "vlc": "vlc.exe",
            "terminal": "WindowsTerminal.exe",
            "cmd": "cmd.exe",
            "powershell": "powershell.exe",
        }
        proc_name = process_map.get(name_lower, f"{name_lower}.exe")
        result = subprocess.run(
            ["taskkill", "/IM", proc_name, "/F"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            return f"Closed {name}."
        if "not found" in result.stderr.lower():
            return f"{name} is not running."
        return f"Could not close {name}: {result.stderr.strip()}"

    # Process image names for apps we can detect as already-running.
    _RUNNING_NAMES = {
        "firefox": "firefox.exe", "chrome": "chrome.exe", "edge": "msedge.exe",
        "notepad": "notepad.exe", "spotify": "Spotify.exe", "discord": "Discord.exe",
        "steam": "steam.exe", "vscode": "Code.exe", "vs code": "Code.exe", "code": "Code.exe",
        "terminal": "WindowsTerminal.exe", "task manager": "Taskmgr.exe", "taskmgr": "Taskmgr.exe",
    }

    def _is_running(self, proc_name: str) -> bool:
        """True if a process with this image name is currently running."""
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"IMAGENAME eq {proc_name}", "/NH", "/FO", "CSV"],
                capture_output=True, text=True, timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return proc_name.lower() in result.stdout.lower()

    def launch_app(self, target: str) -> str:
        app_map = {
            "firefox": "firefox",
            "chrome": "chrome",
            "edge": "msedge",
            "notepad": "notepad",
            "explorer": "explorer",
            "calculator": "calc",
            "settings": "ms-settings:",
            "spotify": "spotify:",
            "discord": "discord:",
            "steam": "steam:",
            "epic games": "com.epicgames.launcher:",
            "epic": "com.epicgames.launcher:",
            "vscode": "code",
            "vs code": "code",
            "terminal": "wt",
            "cmd": "cmd",
            "powershell": "powershell",
            "task manager": "taskmgr",
            "snipping tool": "SnippingTool",
        }
        target_lower = target.lower().strip()
        # Source of truth beats memory: if the app is already running, say so
        # instead of relaunching, so "open X" / "is X open?" reflect reality
        # rather than the model's recollection of a past launch.
        proc_name = self._RUNNING_NAMES.get(target_lower)
        if proc_name and self._is_running(proc_name):
            return f"{target} is already running."
        cmd = app_map.get(target_lower, target)

        # Claude Code is a CLI, not a GUI app, and os.startfile (below) has no
        # way to scrub the inherited environment. Route any claude launch through
        # open_terminal, which strips JARVIS's Anthropic creds so Claude Code uses
        # the user's subscription login instead of the API key.
        if _launches_claude(cmd):
            self.open_terminal(cmd)
            return f"Launched {target} in a terminal."

        # os.startfile -> ShellExecute, which resolves protocols, paths, PATH,
        # and the App Paths registry (how the Run dialog finds browsers).
        # Popen(shell=True) only searched PATH and reported success even when
        # cmd.exe couldn't find the app.
        candidates = [cmd]
        if not cmd.endswith(":") and "." not in os.path.basename(cmd):
            candidates.append(cmd + ".exe")
        for candidate in candidates:
            try:
                os.startfile(candidate)
                return f"Launched {target}."
            except OSError:
                continue
        return f"Could not find '{target}' on this PC — it may not be installed or go by a different name."

    def list_processes(self) -> str:
        result = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=10,
        )
        seen = {}
        for line in result.stdout.strip().split("\n"):
            parts = line.strip('"').split('","')
            if len(parts) >= 2:
                name = parts[0]
                mem = parts[-1].strip('"')
                seen[name] = mem
        top = sorted(seen.items(), key=lambda x: x[0].lower())[:30]
        return "\n".join(f"- {name} ({mem})" for name, mem in top)

    def open_url(self, url: str) -> str:
        os.startfile(url)
        return f"Opened {url} in default browser."

    def run_command(self, command: str) -> tuple[int, str, str]:
        """Run a shell command headlessly and return (exit_code, stdout, stderr).

        The caller (tools/terminal.py) sanitizes the command and the security
        guard requires confirmation first — this method is the raw executor.
        """
        # If the command invokes Claude Code, run it without JARVIS's Anthropic
        # creds so it uses the user's subscription login, not the API key. Every
        # other command inherits the daemon env (env=None -> "inherit").
        env = _scrubbed_env() if _launches_claude(command) else None
        try:
            result = subprocess.run(
                command, shell=True, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=COMMAND_TIMEOUT_SEC,
                env=env,
            )
        except subprocess.TimeoutExpired:
            return 124, "", f"command timed out after {COMMAND_TIMEOUT_SEC}s"
        return result.returncode, result.stdout or "", result.stderr or ""

    def open_terminal(self, command: "str | None" = None) -> None:
        """Open a VISIBLE terminal window, optionally running `command` and
        keeping the window open (so the user can interact — e.g. `claude`).

        Prefers Windows Terminal (`wt`); falls back to a new cmd console.
        """
        creationflags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
        inner = ["cmd", "/k", command] if command else ["cmd"]

        if _launches_claude(command):
            # Claude Code must use the user's subscription login, not JARVIS's
            # API key, so strip the Anthropic credentials from the child's env.
            # Spawn cmd.exe DIRECTLY rather than through `wt`: Windows Terminal's
            # single-instance (monarch/peasant) model can route a new `wt` call
            # into an existing WindowsTerminal.exe whose environment still holds
            # the key, so an env= passed to `wt` is NOT guaranteed to reach the
            # shell. A direct CREATE_NEW_CONSOLE cmd.exe always gets this env.
            subprocess.Popen(inner, creationflags=creationflags, env=_scrubbed_env())
            return

        # Normal terminals inherit the daemon env (env defaults to "inherit").
        # Prefer Windows Terminal, fall back to a new cmd console.
        try:
            subprocess.Popen(["wt", *inner])
            return
        except (OSError, subprocess.SubprocessError):
            pass
        subprocess.Popen(inner, creationflags=creationflags)

    def list_windows(self) -> list[tuple[int, str]]:
        """Return (pid, title) for every visible top-level window with a title.

        Used by the shutdown unsaved-work scan. Best-effort — returns [] if the
        pywin32 window APIs are unavailable.
        """
        try:
            import win32gui
            import win32process
        except ImportError:
            return []
        out: list[tuple[int, str]] = []

        def _cb(hwnd, _):
            if not win32gui.IsWindowVisible(hwnd):
                return
            title = win32gui.GetWindowText(hwnd)
            if not title.strip():
                return
            try:
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
            except Exception:
                pid = 0
            out.append((pid, title))

        win32gui.EnumWindows(_cb, None)
        return out

    def power(self, action: str, grace_seconds: int = 60) -> str:
        """Restart/shutdown (WITHOUT /f, so each app shows its own save prompt)
        or cancel a pending shutdown. Returns the executed description."""
        if action == "cancel":
            subprocess.run(["shutdown", "/a"], capture_output=True, text=True, timeout=10)
            return "Cancelled the pending shutdown."
        flag = "/r" if action == "restart" else "/s"
        subprocess.run(
            ["shutdown", flag, "/t", str(int(grace_seconds))],
            capture_output=True, text=True, timeout=10,
        )
        verb = "Restart" if action == "restart" else "Shutdown"
        return f"{verb} scheduled in {int(grace_seconds)}s (apps will prompt to save)."

    def system_info(self) -> str:
        try:
            import psutil
            cpu = psutil.cpu_percent(interval=1)
            mem = psutil.virtual_memory()
            disk = psutil.disk_usage("C:\\")
            return (
                f"CPU: {cpu}%\n"
                f"RAM: {mem.percent}% ({mem.used // (1024**3)}GB / {mem.total // (1024**3)}GB)\n"
                f"Disk C: {disk.percent}% ({disk.used // (1024**3)}GB / {disk.total // (1024**3)}GB)"
            )
        except ImportError:
            return "psutil not installed — cannot get system info."
