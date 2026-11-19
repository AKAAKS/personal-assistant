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

# Windows shell and core system processes. Killing any of these by image name
# takes down the desktop, taskbar and Start menu (explorer.exe) or the OS
# itself. Both close paths refuse them.
#
# This list exists because close_app("explorer") once ran
# `taskkill /IM explorer.exe /F`. By default Windows does NOT give File
# Explorer folder windows their own process, so "close File Explorer" killed
# the shell: black desktop, no taskbar, Alt-Tab the only way to navigate.
_PROTECTED_PROCESSES = frozenset({
    "explorer.exe", "winlogon.exe", "csrss.exe", "wininit.exe", "smss.exe",
    "services.exe", "lsass.exe", "dwm.exe", "svchost.exe", "sihost.exe",
    "ctfmon.exe", "taskhostw.exe", "fontdrvhost.exe", "runtimebroker.exe",
    "shellexperiencehost.exe", "startmenuexperiencehost.exe", "searchhost.exe",
})

# Window classes that ARE the shell rather than an app window. explorer.exe
# owns both these and ordinary folder windows (CabinetWClass / ExploreWClass);
# only the latter may be sent WM_CLOSE.
_SHELL_WINDOW_CLASSES = frozenset({
    "progman", "shell_traywnd", "shell_secondarytraywnd", "workerw", "button",
})


# Schemes open_url may hand to ShellExecute. Everything else (a path, a
# file:// URL, a bare .bat) would be EXECUTED, not browsed.
_WEB_URL_SCHEMES = ("http://", "https://", "mailto:")


def _process_family(proc) -> "set[int]":
    """PIDs of the JARVIS process family around `proc`.

    Climbs to the topmost ancestor still running under the same image name,
    then takes that ancestor and every descendant. tray_app spawns the daemon
    and the voice client as separate children, so from the daemon the voice
    client is a SIBLING - neither an ancestor nor a child. Walking the family
    from the shared root catches it, while the same-image stop keeps us from
    climbing into explorer.exe and adopting every unrelated app.

    `proc` is a psutil.Process (or anything with the same .pid/.name()/
    .parents()/.children() shape).
    """
    own_name = (proc.name() or "").lower()
    root = proc
    for ancestor in proc.parents():          # nearest first
        if (ancestor.name() or "").lower() != own_name:
            break
        root = ancestor
    pids = {proc.pid, root.pid}
    pids.update(c.pid for c in root.children(recursive=True))
    return pids


def _is_shell_window(class_name: str) -> bool:
    """True when this window class is part of the shell (desktop/taskbar)."""
    return (class_name or "").strip().lower() in _SHELL_WINDOW_CLASSES


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

    # Spoken app name -> process image name. Aliases matter: the model says
    # whatever the user said, and "file explorer" must resolve the same way
    # "explorer" does so the shell guard below sees it.
    _PROCESS_MAP = {
        "firefox": "firefox.exe",
        "chrome": "chrome.exe",
        "edge": "msedge.exe",
        "notepad": "notepad.exe",
        "explorer": "explorer.exe",
        "file explorer": "explorer.exe",
        "windows explorer": "explorer.exe",
        "file manager": "explorer.exe",
        "files": "explorer.exe",
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

    def _resolve_process(self, name: str) -> str:
        """Spoken name -> process image name ("file explorer" -> explorer.exe)."""
        key = (name or "").lower().strip()
        mapped = self._PROCESS_MAP.get(key)
        if mapped:
            return mapped
        return key if key.endswith(".exe") else f"{key}.exe"

    def _pids_for_image(self, proc_name: str) -> "set[int]":
        """PIDs currently running under this image name. Empty on any failure."""
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"IMAGENAME eq {proc_name}", "/NH", "/FO", "CSV"],
                capture_output=True, text=True, timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            return set()
        pids = set()
        for line in (result.stdout or "").splitlines():
            parts = [p.strip('"') for p in line.strip().split('","')]
            if len(parts) >= 2 and parts[0].strip('"').lower() == proc_name.lower():
                try:
                    pids.add(int(parts[1]))
                except ValueError:
                    continue
        return pids

    def _own_pids(self) -> "set[int]":
        """PIDs that ARE JARVIS: this process and the rest of its family.

        The daemon, the voice client and the tray all run as pythonw.exe
        (clients/tray_app.py), so a kill aimed at the image name "python"
        would take the assistant down mid-sentence - the File Explorer bug
        pointed at ourselves. Identity is by PID, never by name: sys.executable
        is unreliable here (a Windows Store python.exe is an alias stub whose
        real image name is python3.11.exe). Best-effort: psutil is optional.
        """
        pids = {os.getpid()}
        try:
            import psutil
        except ImportError:
            return pids
        try:
            pids |= _process_family(psutil.Process(os.getpid()))
        except Exception:
            pass
        return pids

    def _terminate(self, proc_name: str, label: str, force: bool) -> str:
        """Terminate a process BY PID, never by image name.

        `taskkill /IM` hits every process sharing the name - that is what
        killed the whole shell for one folder window, and what would kill the
        daemon, the voice client and the tray together. Resolving to PIDs
        first keeps the blast radius to what was actually asked for, and lets
        us drop our own PIDs from the list.
        """
        pids = self._pids_for_image(proc_name)
        if not pids:
            return f"{label} is not running."
        targets = sorted(pids - self._own_pids())
        if not targets:
            return (
                f"{label} is JARVIS's own process ({proc_name}) - closing it "
                "would shut myself down mid-sentence. Quit me from the tray "
                "icon instead."
            )

        failures = []
        for pid in targets:
            cmd = ["taskkill", "/PID", str(pid)] + (["/F"] if force else [])
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            except (OSError, subprocess.SubprocessError) as exc:
                failures.append(str(exc))
                continue
            if result.returncode != 0:
                failures.append((result.stderr or "").strip())
        if not failures:
            return f"Closed {label}." if not force else f"Force-closed {label}."
        if all("not found" in f.lower() for f in failures):
            return f"{label} is not running."
        detail = failures[0]
        if force:
            return f"Could not force-close {label}: {detail}"
        return (
            f'Could not close {label}: {detail} - say "force close {label}" '
            "to kill it outright, which loses anything unsaved."
        )

    def _close_windows(self, proc_name: str) -> int:
        """Post WM_CLOSE to this process's ordinary windows; return how many.

        WM_CLOSE is what clicking the X does: the app gets to prompt about
        unsaved work and decide for itself. Shell windows (desktop, taskbar)
        are skipped, so closing a folder window never disturbs the shell.
        Best-effort - returns 0 if pywin32 is unavailable.
        """
        pids = self._pids_for_image(proc_name)
        if not pids:
            return 0
        try:
            import win32con
            import win32gui
            import win32process
        except ImportError:
            return 0

        targets: "list[int]" = []

        def _cb(hwnd, _):
            if not win32gui.IsWindowVisible(hwnd):
                return
            try:
                if _is_shell_window(win32gui.GetClassName(hwnd)):
                    return
                if not win32gui.GetWindowText(hwnd).strip():
                    return
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
            except Exception:
                return
            if pid in pids:
                targets.append(hwnd)

        try:
            win32gui.EnumWindows(_cb, None)
        except Exception:
            return 0
        for hwnd in targets:
            try:
                win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
            except Exception:
                continue
        return len(targets)

    def close_app(self, name: str) -> str:
        """Close an app the way clicking its X does - never a force-kill.

        Order: close its windows (app may prompt to save) -> graceful taskkill
        for windowless/tray apps -> tell the user force_close_app exists. The
        shell is never terminated by image name at all.
        """
        proc_name = self._resolve_process(name)
        label = (name or "").strip() or proc_name

        closed = self._close_windows(proc_name)
        if closed == 1:
            return f"Closed {label}."
        if closed > 1:
            return f"Closed {closed} {label} windows."

        if proc_name in _PROTECTED_PROCESSES:
            return (
                f"{label} has no open windows to close. I won't terminate "
                f"{proc_name} - it is the Windows shell, and killing it blanks "
                "the desktop, taskbar and Start menu."
            )

        # No window to close: a tray/background app. Terminate by PID, and
        # without /F, so the process still gets to exit cleanly.
        return self._terminate(proc_name, label, force=False)

    def force_close_app(self, name: str) -> str:
        """Force-kill an app (taskkill /F). Destructive: unsaved work is lost.

        Refuses shell and core system processes no matter how it is asked.
        """
        proc_name = self._resolve_process(name)
        label = (name or "").strip() or proc_name
        if proc_name in _PROTECTED_PROCESSES:
            return (
                f"Refusing to force-kill {proc_name} - it is a Windows shell or "
                "core system process. Killing it blanks the desktop, taskbar and "
                "Start menu, leaving only Alt-Tab to switch windows. To close a "
                f"File Explorer window instead, ask me to close {label} normally."
            )
        return self._terminate(proc_name, label, force=True)

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
        """Open an http(s) or mailto URL in the default browser.

        os.startfile is ShellExecute: handed a path it RUNS the file. This tool
        sits at the `write` tier (no confirmation), so without a scheme check
        it is arbitrary execution that walks around run_command's destructive
        tier and its sanitizer. Anything that is not a web URL is refused.
        """
        target = (url or "").strip()
        if not target.lower().startswith(_WEB_URL_SCHEMES):
            return (
                f"'{target}' is not a web URL. This tool opens http(s) and "
                "mailto links only - use launch_app to start a program."
            )
        try:
            os.startfile(target)
        except OSError as exc:
            return f"Could not open {target}: {exc}"
        return f"Opened {target} in default browser."

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
