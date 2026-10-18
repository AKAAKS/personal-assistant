"""Local system control — launch/close apps, system info, clipboard."""

import os
import subprocess


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
