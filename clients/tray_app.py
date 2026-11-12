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
import math
import sys
import time

from clients import activity

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

# The palette is split by meaning, and the two halves never overlap in time.
#
# SUPERVISORY states mean "JARVIS is not available to talk": grey for
# transitional, orange for deliberately off, red for broken. CONVERSATIONAL
# states are sub-states of LISTENING -- they only exist while it IS available --
# so they had to stay out of the grey/orange/red families entirely. That leaves
# violet, green and magenta, which nothing else uses.
#
# HEARING deliberately reuses the armed cyan and differs by GLYPH (filled ring)
# rather than hue: it is the same activity, one step further along, and a sixth
# colour would have been noise.
#
# At 16px motion reads better than hue, so the two states that must never be
# misread carry it: THINKING pulses and MISSED blinks. That -- not their hue --
# is what keeps magenta apart from red, since every red state is steady.
STATE_COLORS = {
    STARTING: (120, 120, 120),
    LISTENING: (0, 180, 200),
    PAUSED: (220, 160, 0),
    BRAIN_DOWN: (200, 40, 40),
    # Was identical to BRAIN_DOWN, so you could not tell which half had broken:
    # the daemon being unreachable is actionable, a crashed client is not.
    ERROR: (140, 20, 60),
    STOPPED: (90, 90, 90),
    activity.HEARING: (0, 180, 200),
    activity.THINKING: (150, 90, 230),
    activity.SPEAKING: (40, 200, 80),
    activity.MISSED: (235, 70, 140),
}
STATE_LABEL = {
    STARTING: "JARVIS — starting…",
    LISTENING: "JARVIS — listening",
    PAUSED: "JARVIS — paused (mic off)",
    BRAIN_DOWN: "JARVIS — brain not reachable",
    ERROR: "JARVIS — voice client error",
    STOPPED: "JARVIS — stopped",
    activity.HEARING: "JARVIS — hearing you…",
    activity.THINKING: "JARVIS — thinking…",
    activity.SPEAKING: "JARVIS — speaking",
    activity.MISSED: "JARVIS — didn't catch that",
}

# Only HEARING fills the ring; everything else keeps the hollow arc-reactor look.
_FILLED_STATES = frozenset({activity.HEARING})

# How long a MISSED flash stays up before falling back to armed. Long enough to
# notice if you glance over, short enough not to linger as a false alarm.
MISSED_HOLD_SEC = 1.6

# THINKING is dropped after this. It must outlast a genuinely slow multi-tool
# turn, while still releasing if the client dies mid-turn.
THINKING_MAX_SEC = 60.0

# Pulse: one full cycle per second. PULSE_MIN is how far it blends toward
# white at the trough (1.0 = the base colour, lower = paler), chosen so the
# dot stays obviously lit against a dark taskbar at every phase.
PULSE_PERIOD_SEC = 1.0
PULSE_MIN = 0.35

# Blink geometry for MISSED. One blink = couldn't make out words, two = that
# didn't sound like you.
BLINK_ON_SEC = 0.20
BLINK_OFF_SEC = 0.16
_BLINKS = {activity.NO_WORDS: 1, activity.UNVERIFIED: 2}

# How often the icon re-reads the activity file. Independent of
# TICK_INTERVAL_SEC: process supervision is a slow watchdog, but the indicator
# has to feel immediate, and re-reading one cached file is nearly free.
POLL_INTERVAL_SEC = 0.2

# States whose appearance changes over time, so their cache key includes phase.
_ANIMATED_STATES = frozenset({activity.THINKING, activity.MISSED})


def effective_state(sup_state, act, now):
    """Which state the icon should render.

    Supervisory states always win: if the mic is off or the daemon is down, a
    leftover conversational record must not claim otherwise.
    """
    if sup_state != LISTENING:
        return sup_state
    if act is None:
        return LISTENING
    state = act.get("state")
    if state in (None, activity.ARMED):
        return LISTENING
    age = now - act.get("ts", now)
    if state == activity.MISSED:
        return activity.MISSED if age <= MISSED_HOLD_SEC else LISTENING
    if state == activity.THINKING and age > THINKING_MAX_SEC:
        return LISTENING  # the writer is wedged or gone; stop claiming progress
    return state


def is_filled(state) -> bool:
    """Whether the icon's inner ring is filled in rather than hollow."""
    return state in _FILLED_STATES


def brightness(state, elapsed: float) -> float:
    """Fill-intensity multiplier in [PULSE_MIN, 1.0]; 1.0 for steady states."""
    if state != activity.THINKING:
        return 1.0
    phase = (elapsed % PULSE_PERIOD_SEC) / PULSE_PERIOD_SEC
    # Raised cosine: smooth, and equal to 1.0 at phase 0 so a turn starts lit.
    wave = (math.cos(2.0 * math.pi * phase) + 1.0) / 2.0
    return PULSE_MIN + (1.0 - PULSE_MIN) * wave


def blink_count(reason) -> int:
    """Blinks for a MISSED reason. Unknown reasons blink once."""
    return _BLINKS.get(reason, 1)


def blink_on(elapsed: float, blinks: int) -> bool:
    """Whether the MISSED colour is showing at `elapsed` into the flash."""
    cycle = BLINK_ON_SEC + BLINK_OFF_SEC
    if elapsed < 0 or elapsed >= cycle * blinks:
        return False  # past the pattern: hold dark until MISSED_HOLD_SEC ends
    return (elapsed % cycle) < BLINK_ON_SEC

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
    # The client now configures its own rotating file logging
    # (logs/voice_client.log), so its PIPES go to a SEPARATE file. Pointing
    # both at one path would give that file two writers, and a second raw
    # handle blocks the rotation rename on Windows. The pipes still matter:
    # the client's user-facing print()s ("You: ...", "JARVIS: ...") have
    # nowhere else to go under pythonw.
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    out = _log_file("voice_client.out.log")
    return subprocess.Popen(
        [_pythonw(), "-m", "clients.voice_client"],
        cwd=ROOT, stdout=out, stderr=subprocess.STDOUT, creationflags=flags,
    )


def _make_icon_image(state, *, elapsed: float = 0.0, reason=None):
    """Render the tray dot for `state`.

    `elapsed` drives motion (the THINKING pulse and the MISSED blink) and is
    seconds since that state began. A MISSED frame between blinks falls back to
    the armed look, so the flash reads as blinking over the resting icon rather
    than the icon disappearing.
    """
    from PIL import Image, ImageDraw

    if state == activity.MISSED and not blink_on(elapsed, blink_count(reason)):
        state = LISTENING  # the dark half of a blink

    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    r, g, b = STATE_COLORS.get(state, (120, 120, 120))
    level = brightness(state, elapsed)
    # Pulse by blending toward WHITE rather than toward black. The taskbar is
    # dark, so a dimming dot drops out of sight at the trough and reads as a
    # glitch instead of a heartbeat; brightening keeps it legible throughout.
    color = tuple(int(c + (255 - c) * (1.0 - level)) for c in (r, g, b))
    d.ellipse([4, 4, size - 4, size - 4], fill=color)
    # a simple arc-reactor "ring" so it reads as JARVIS, not a generic dot
    ring = [20, 20, size - 20, size - 20]
    if is_filled(state):
        # HEARING: ring filled in -- "taking this in" -- so it is distinct from
        # armed without spending another colour on it.
        d.ellipse(ring, fill=(255, 255, 255, 230))
    else:
        d.ellipse(ring, outline=(255, 255, 255, 230), width=4)
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

    # Last frame actually pushed to the tray, so a 5Hz poll does not redraw an
    # unchanged icon (pystray re-creates the native icon on every assignment).
    shown = {"key": None}

    def refresh(icon, *, force=False):
        act = activity.read()
        now = time.time()
        state = effective_state(sup.state, act, now)
        reason = act.get("reason") if act else None
        began = act.get("ts", now) if act else now
        elapsed = max(0.0, now - began)
        # Quantise the pulse so a steady state is one cache key, not 5 a second.
        phase = round(elapsed, 1) if state in _ANIMATED_STATES else 0.0
        key = (state, reason, phase)
        if not force and key == shown["key"]:
            return
        shown["key"] = key
        icon.icon = _make_icon_image(state, elapsed=elapsed, reason=reason)
        icon.title = STATE_LABEL.get(state, "JARVIS")
        icon.update_menu()

    def on_pause_toggle(icon, _item):
        if sup.paused:
            sup.start_listening()
        else:
            sup.stop_listening()
        refresh(icon, force=True)

    def on_restart(icon, _item):
        sup.restart()
        refresh(icon, force=True)

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
        pystray.MenuItem(
            lambda _i: STATE_LABEL.get(
                effective_state(sup.state, activity.read(), time.time()), "JARVIS"
            ),
            None,
            enabled=False,
        ),
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
        refresh(icon, force=True)
        # Two cadences in one loop: the watchdog is a slow, relatively expensive
        # check (a TCP connect plus a process poll), while the indicator must
        # feel immediate. Poll fast, supervise on the original interval.
        next_tick = time.monotonic() + TICK_INTERVAL_SEC
        while not stop.wait(POLL_INTERVAL_SEC):
            if time.monotonic() >= next_tick:
                sup.tick()
                next_tick = time.monotonic() + TICK_INTERVAL_SEC
            refresh(icon)

    icon.run(setup=setup)


if __name__ == "__main__":
    run()
