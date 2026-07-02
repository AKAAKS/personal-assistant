"""Android TV / Google TV control via ADB (Android Debug Bridge).

Requires:
  - ADB installed on your machine (comes with Android SDK platform-tools)
  - Developer mode enabled on your Android TV
  - ADB debugging enabled over network on the TV
  - First connection requires confirming the RSA key on the TV screen

Uses the `adb` command-line tool via subprocess for reliability.
"""

import subprocess
import shlex


# Common app package names for quick launch
KNOWN_APPS = {
    "netflix": "com.netflix.ninja",
    "youtube": "com.google.android.youtube.tv",
    "disney": "com.disney.disneyplus",
    "disney+": "com.disney.disneyplus",
    "prime": "com.amazon.amazonvideo.livingroom",
    "amazon prime": "com.amazon.amazonvideo.livingroom",
    "hulu": "com.hulu.livingroomplus",
    "hbo": "com.hbo.hbomax",
    "hbo max": "com.hbo.hbomax",
    "max": "com.hbo.hbomax",
    "plex": "com.plexapp.android",
    "spotify": "com.spotify.tv.android",
    "twitch": "tv.twitch.android.app",
    "kodi": "org.xbmc.kodi",
    "chrome": "com.android.chrome",
    "settings": "com.android.tv.settings",
}

# ADB keycodes for remote control
KEYCODES = {
    "up": "KEYCODE_DPAD_UP",
    "down": "KEYCODE_DPAD_DOWN",
    "left": "KEYCODE_DPAD_LEFT",
    "right": "KEYCODE_DPAD_RIGHT",
    "select": "KEYCODE_DPAD_CENTER",
    "enter": "KEYCODE_ENTER",
    "back": "KEYCODE_BACK",
    "home": "KEYCODE_HOME",
    "menu": "KEYCODE_MENU",
    "play": "KEYCODE_MEDIA_PLAY",
    "pause": "KEYCODE_MEDIA_PAUSE",
    "play_pause": "KEYCODE_MEDIA_PLAY_PAUSE",
    "stop": "KEYCODE_MEDIA_STOP",
    "next": "KEYCODE_MEDIA_NEXT",
    "previous": "KEYCODE_MEDIA_PREVIOUS",
    "volume_up": "KEYCODE_VOLUME_UP",
    "volume_down": "KEYCODE_VOLUME_DOWN",
    "mute": "KEYCODE_VOLUME_MUTE",
    "power": "KEYCODE_POWER",
    "search": "KEYCODE_SEARCH",
    "tab": "KEYCODE_TAB",
    "delete": "KEYCODE_DEL",
}


class AndroidTVClient:
    def __init__(self, tv_ip: str, tv_port: int = 5555):
        self.tv_address = f"{tv_ip}:{tv_port}"
        self._connected = False

    def _run_adb(self, *args: str) -> str:
        """Run an ADB command and return stdout."""
        cmd = ["adb", "-s", self.tv_address, *args]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if result.returncode != 0 and result.stderr:
            raise RuntimeError(f"ADB error: {result.stderr.strip()}")
        return result.stdout.strip()

    # --- Connection ---

    def connect(self) -> str:
        """Connect to the Android TV over the network."""
        result = subprocess.run(
            ["adb", "connect", self.tv_address],
            capture_output=True, text=True, timeout=10,
        )
        output = result.stdout.strip()
        if "connected" in output.lower():
            self._connected = True
            return f"Connected to {self.tv_address}"
        elif "already connected" in output.lower():
            self._connected = True
            return f"Already connected to {self.tv_address}"
        else:
            raise RuntimeError(f"Failed to connect: {output}. Make sure ADB debugging is enabled on your TV.")

    def disconnect(self) -> str:
        result = subprocess.run(
            ["adb", "disconnect", self.tv_address],
            capture_output=True, text=True, timeout=10,
        )
        self._connected = False
        return result.stdout.strip()

    def is_connected(self) -> bool:
        try:
            output = self._run_adb("get-state")
            return output.strip() == "device"
        except (RuntimeError, subprocess.TimeoutExpired):
            return False

    # --- App control ---

    def launch_app(self, app_name: str) -> str:
        """Launch an app by common name or package name.

        Supports names like 'netflix', 'youtube', 'disney+', etc.
        Also accepts full package names like 'com.netflix.ninja'.
        """
        package = KNOWN_APPS.get(app_name.lower(), app_name)
        # Use monkey to launch the app's main activity
        self._run_adb("shell", "monkey", "-p", package, "-c",
                       "android.intent.category.LEANBACK_LAUNCHER", "1")
        friendly = app_name if app_name.lower() in KNOWN_APPS else package
        return f"Launched {friendly}"

    def close_app(self, app_name: str) -> str:
        """Force-stop an app."""
        package = KNOWN_APPS.get(app_name.lower(), app_name)
        self._run_adb("shell", "am", "force-stop", package)
        return f"Closed {app_name}"

    def get_current_app(self) -> str:
        """Get the package name of the currently focused app."""
        output = self._run_adb("shell", "dumpsys", "window", "windows")
        for line in output.splitlines():
            if "mCurrentFocus" in line or "mFocusedApp" in line:
                # Extract package name from the line
                parts = line.split()
                for part in parts:
                    if "/" in part and "." in part:
                        return part.split("/")[0].strip("{}")
        return "unknown"

    def list_installed_apps(self) -> list[str]:
        """List all installed packages."""
        output = self._run_adb("shell", "pm", "list", "packages", "-3")
        return [line.replace("package:", "") for line in output.splitlines()]

    # --- Remote control ---

    def send_key(self, key: str) -> str:
        """Send a remote control key press.

        Accepts: up, down, left, right, select, enter, back, home, menu,
                 play, pause, play_pause, stop, next, previous,
                 volume_up, volume_down, mute, power, search
        """
        keycode = KEYCODES.get(key.lower())
        if not keycode:
            return f"Unknown key: {key}. Available: {', '.join(KEYCODES.keys())}"
        self._run_adb("shell", "input", "keyevent", keycode)
        return f"Sent key: {key}"

    def send_keys(self, keys: list[str], delay_ms: int = 300) -> str:
        """Send multiple key presses in sequence."""
        results = []
        for key in keys:
            results.append(self.send_key(key))
        return "; ".join(results)

    # --- Text input ---

    def type_text(self, text: str) -> str:
        """Type text into the currently focused input field.

        Useful for searching within apps (Netflix search, YouTube search, etc.).
        """
        # ADB input text requires spaces to be escaped
        escaped = text.replace(" ", "%s").replace("'", "\\'").replace('"', '\\"')
        self._run_adb("shell", "input", "text", escaped)
        return f"Typed: {text}"

    def clear_text(self, char_count: int = 50) -> str:
        """Clear text in the current input field by sending delete keys."""
        for _ in range(char_count):
            self._run_adb("shell", "input", "keyevent", "KEYCODE_DEL")
        return f"Cleared up to {char_count} characters"

    # --- Deep linking ---

    def open_url(self, url: str) -> str:
        """Open a URL on the TV (e.g., a YouTube video link)."""
        self._run_adb("shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", url)
        return f"Opened URL: {url}"

    def search_youtube(self, query: str) -> str:
        """Deep-link a YouTube search on the TV."""
        from urllib.parse import quote
        url = f"https://www.youtube.com/results?search_query={quote(query)}"
        return self.open_url(url)

    # --- System ---

    def get_screen_state(self) -> str:
        """Check if the TV screen is on or off."""
        output = self._run_adb("shell", "dumpsys", "power")
        for line in output.splitlines():
            if "mWakefulness=" in line:
                return "on" if "Awake" in line else "off"
        return "unknown"

    def wake_up(self) -> str:
        """Wake up the TV if it's in standby."""
        self._run_adb("shell", "input", "keyevent", "KEYCODE_WAKEUP")
        return "Sent wake-up signal"

    def sleep(self) -> str:
        """Put the TV to sleep."""
        self._run_adb("shell", "input", "keyevent", "KEYCODE_SLEEP")
        return "Sent sleep signal"
