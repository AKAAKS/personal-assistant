"""System-tray launcher for JARVIS — the no-terminal way to run it.

Two layers:

- ``JarvisSupervisor`` — the testable core. It makes sure the brain (jarvisd)
  is up, keeps the voice client alive, and exposes pause/resume/restart/quit.
  All of its collaborators (port probe, process spawners) are injected, so the
  logic is covered by tests/test_tray_supervisor.py with fakes.
- A thin pystray shell (``run``) that draws the tray icon, wires the menu to the
  supervisor, and runs the watchdog tick on a timer. GUI-only, not unit-tested.
  pystray/Pillow are imported lazily so this module imports fine without them.

Run (normally via the Startup shortcut created by scripts/install_tray.bat):
    pythonw -m clients.tray_app
"""

import os
import socket
import subprocess
import sys
import time

# --- states the icon reflects ---
STARTING = "starting"
LISTENING = "listening"
PAUSED = "paused"
BRAIN_DOWN = "brain_down"
ERROR = "error"
STOPPED = "stopped"


class JarvisSupervisor:
    """Supervises the daemon + voice client. Pure logic; collaborators injected.

    Args:
        is_port_open: callable() -> bool, True when the daemon port is reachable.
        spawn_daemon: callable() -> process handle (has poll()/terminate()).
        spawn_client: callable() -> process handle for the voice client.
        daemon_timeout: seconds to wait for the daemon port after spawning it.
        max_relaunch: consecutive client relaunches before giving up (-> ERROR).
        sleep/clock: injected for deterministic tests.
    """

    def __init__(self, *, is_port_open, spawn_daemon, spawn_client,
                 daemon_timeout=45.0, max_relaunch=3,
                 sleep=time.sleep, clock=time.monotonic):
        self.is_port_open = is_port_open
        self.spawn_daemon = spawn_daemon
        self.spawn_client = spawn_client
        self.daemon_timeout = daemon_timeout
        self.max_relaunch = max_relaunch
        self._sleep = sleep
        self._clock = clock

        self.state = STARTING
        self.paused = False
        self.started_daemon = False
        self._daemon = None
        self._client = None
        self._relaunch_count = 0

    # -- daemon --

    def ensure_daemon(self) -> bool:
        """Return True once the daemon port is reachable, starting it if needed."""
        if self.is_port_open():
            self.started_daemon = False
            return True
        self._daemon = self.spawn_daemon()
        self.started_daemon = True
        deadline = self._clock() + self.daemon_timeout
        while self._clock() < deadline:
            if self.is_port_open():
                return True
            self._sleep(1.0)
        self.state = BRAIN_DOWN
        return False

    # -- listening (the voice client) --

    def _client_alive(self) -> bool:
        return self._client is not None and self._client.poll() is None

    def start_listening(self) -> None:
        """(Re)start the voice client and clear the paused flag."""
        self.paused = False
        self._relaunch_count = 0
        if not self._client_alive():
            self._client = self.spawn_client()
        self.state = LISTENING

    def stop_listening(self) -> None:
        """Pause: terminate the voice client (releases the mic + GPU) and latch paused."""
        self.paused = True
        if self._client_alive():
            self._client.terminate()
        self._client = None
        self.state = PAUSED

    def restart(self) -> None:
        self.stop_listening()
        self.start_listening()

    # -- watchdog --

    def tick(self) -> None:
        """Called on a timer: keep the voice client alive unless paused."""
        if self.paused:
            return
        if not self.is_port_open():
            self.state = BRAIN_DOWN
            return
        if self._client_alive():
            self._relaunch_count = 0
            self.state = LISTENING
            return
        # client is dead and we're not paused -> relaunch, within a cap
        if self._relaunch_count >= self.max_relaunch:
            self.state = ERROR
            return
        self._client = self.spawn_client()
        self._relaunch_count += 1
        self.state = LISTENING

    # -- shutdown --

    def quit(self) -> None:
        """Stop the voice client always; stop the daemon only if we started it."""
        if self._client_alive():
            self._client.terminate()
        self._client = None
        if self.started_daemon and self._daemon is not None and self._daemon.poll() is None:
            self._daemon.terminate()
        self.state = STOPPED


# ---------------------------------------------------------------------------
# Real collaborators + pystray shell (GUI-only; not unit-tested)
# ---------------------------------------------------------------------------

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = os.path.join(ROOT, "logs")

_STATE_COLORS = {
    STARTING: (120, 120, 120),
    LISTENING: (0, 180, 200),
    PAUSED: (220, 160, 0),
    BRAIN_DOWN: (200, 40, 40),
    ERROR: (200, 40, 40),
    STOPPED: (90, 90, 90),
}
_STATE_LABEL = {
    STARTING: "JARVIS — starting…",
    LISTENING: "JARVIS — listening",
    PAUSED: "JARVIS — paused (mic off)",
    BRAIN_DOWN: "JARVIS — brain not reachable",
    ERROR: "JARVIS — voice client error",
    STOPPED: "JARVIS — stopped",
}

# How often the watchdog re-checks the daemon port + voice-client process. Each
# daemon-port check is a short TCP connect; keep it infrequent so it neither
# churns the event loop nor spams the daemon log (a crashed voice client is still
# relaunched within this window, which is fine for a rare event).
TICK_INTERVAL_SEC = 15.0


def _pythonw() -> str:
    """The windowless interpreter, so spawned children show no console."""
    exe = sys.executable or "python"
    cand = os.path.join(os.path.dirname(exe), "pythonw.exe")
    return cand if os.path.exists(cand) else exe


def _daemon_endpoint():
    host = os.getenv("JARVIS_HOST", "127.0.0.1")
    port = int(os.getenv("JARVIS_PORT", "8765"))
    return host, port


def _port_open() -> bool:
    host, port = _daemon_endpoint()
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def _log_file(name: str):
    os.makedirs(LOG_DIR, exist_ok=True)
    return open(os.path.join(LOG_DIR, name), "a", encoding="utf-8")


def _spawn_daemon():
    # Mirror scripts/jarvisd_launcher.py: pythonw running the jarvisd package,
    # rooted at the repo so data/ and logs/ resolve. jarvisd configures its own
    # file logging; we don't capture its pipes.
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.Popen([_pythonw(), "-m", "jarvisd"], cwd=ROOT, creationflags=flags)


def _spawn_client():
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    out = _log_file("voice_client.log")
    return subprocess.Popen(
        [_pythonw(), "-m", "clients.voice_client"],
        cwd=ROOT, stdout=out, stderr=subprocess.STDOUT, creationflags=flags,
    )


def _make_icon_image(state):
    from PIL import Image, ImageDraw
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    color = _STATE_COLORS.get(state, (120, 120, 120))
    d.ellipse([4, 4, size - 4, size - 4], fill=color)
    # a simple arc-reactor "ring" so it reads as JARVIS, not a generic dot
    d.ellipse([20, 20, size - 20, size - 20], outline=(255, 255, 255, 230), width=4)
    return img


def _single_instance_lock():
    """Return a handle if we acquired the lock, else None (another tray runs)."""
    try:
        import win32api
        import win32event
        import winerror
        h = win32event.CreateMutex(None, False, "Global\\JarvisTrayApp")
        if win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS:
            return None
        return h
    except Exception:
        # pywin32 missing or non-Windows: skip the guard rather than fail.
        return object()


def run():
    lock = _single_instance_lock()
    if lock is None:
        return  # another tray instance already owns the mutex

    import threading
    import pystray

    sup = JarvisSupervisor(
        is_port_open=_port_open,
        spawn_daemon=_spawn_daemon,
        spawn_client=_spawn_client,
    )
    stop = threading.Event()

    def refresh(icon):
        icon.icon = _make_icon_image(sup.state)
        icon.title = _STATE_LABEL.get(sup.state, "JARVIS")
        icon.update_menu()

    def on_pause_toggle(icon, _item):
        if sup.paused:
            sup.start_listening()
        else:
            sup.stop_listening()
        refresh(icon)

    def on_restart(icon, _item):
        sup.restart()
        refresh(icon)

    def on_open_logs(_icon, _item):
        os.makedirs(LOG_DIR, exist_ok=True)
        try:
            os.startfile(LOG_DIR)  # noqa: S606 (Windows-only, intentional)
        except Exception:
            pass

    def on_quit(icon, _item):
        stop.set()
        sup.quit()
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem(lambda _i: _STATE_LABEL.get(sup.state, "JARVIS"), None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(
            lambda _i: "Resume listening" if sup.paused else "Pause listening",
            on_pause_toggle,
        ),
        pystray.MenuItem("Restart", on_restart),
        pystray.MenuItem("Open logs", on_open_logs),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", on_quit),
    )

    icon = pystray.Icon("jarvis", _make_icon_image(STARTING), "JARVIS — starting…", menu)

    def setup(icon):
        icon.visible = True
        sup.ensure_daemon()
        if sup.state != BRAIN_DOWN:
            sup.start_listening()
        refresh(icon)
        while not stop.wait(TICK_INTERVAL_SEC):
            sup.tick()
            refresh(icon)

    icon.run(setup=setup)


if __name__ == "__main__":
    run()
