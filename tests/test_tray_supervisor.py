"""JarvisSupervisor — the testable core behind the system-tray launcher.

The pystray UI shell in clients/tray_app.py is thin and GUI-only (not tested);
all start/stop/watchdog/quit logic lives in JarvisSupervisor and is covered here
with fake process handles and a fake port probe (no real sockets or subprocesses).
"""

from clients.tray_app import JarvisSupervisor


class FakeProc:
    """Stands in for subprocess.Popen: poll() + terminate()."""

    def __init__(self, alive=True):
        self._alive = alive
        self.terminated = False

    def poll(self):
        return None if self._alive else 0

    def terminate(self):
        self.terminated = True
        self._alive = False

    def die(self):
        self._alive = False


class Spawner:
    """Records spawn calls and hands back FakeProcs (newest kept for inspection)."""

    def __init__(self, alive=True):
        self.calls = 0
        self.procs = []
        self._alive = alive

    def __call__(self):
        self.calls += 1
        p = FakeProc(alive=self._alive)
        self.procs.append(p)
        return p

    @property
    def last(self):
        return self.procs[-1] if self.procs else None


class Port:
    """Fake daemon-port probe: a fixed flag, or a scripted sequence of returns."""

    def __init__(self, open_=False, seq=None):
        self.open = open_
        self.seq = list(seq) if seq is not None else None

    def __call__(self):
        if self.seq:
            return self.seq.pop(0)
        return self.open


def _make(port, daemon=None, client=None, **kw):
    return JarvisSupervisor(
        is_port_open=port,
        spawn_daemon=daemon or Spawner(),
        spawn_client=client or Spawner(),
        sleep=lambda _s: None,
        **kw,
    )


# --- ensure_daemon ---

def test_ensure_daemon_skips_spawn_when_already_up():
    daemon = Spawner()
    sup = _make(Port(open_=True), daemon=daemon)
    assert sup.ensure_daemon() is True
    assert daemon.calls == 0
    assert sup.started_daemon is False


def test_ensure_daemon_spawns_when_down_then_waits():
    daemon = Spawner()
    # down on the first probe, up on the next
    sup = _make(Port(seq=[False, True]), daemon=daemon)
    assert sup.ensure_daemon() is True
    assert daemon.calls == 1
    assert sup.started_daemon is True


def test_ensure_daemon_times_out_when_port_never_opens():
    ticks = iter([0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 60.0, 61.0])
    sup = _make(Port(open_=False), daemon=Spawner(), daemon_timeout=5.0,
                clock=lambda: next(ticks))
    assert sup.ensure_daemon() is False
    assert sup.state == "brain_down"


# --- start / pause / resume ---

def test_start_listening_spawns_client():
    client = Spawner()
    sup = _make(Port(open_=True), client=client)
    sup.start_listening()
    assert client.calls == 1
    assert sup.paused is False
    assert sup.state == "listening"


def test_pause_terminates_client_and_sets_paused():
    client = Spawner()
    sup = _make(Port(open_=True), client=client)
    sup.start_listening()
    sup.stop_listening()
    assert client.last.terminated is True
    assert sup.paused is True
    assert sup.state == "paused"


def test_resume_respawns_the_client():
    client = Spawner()
    sup = _make(Port(open_=True), client=client)
    sup.start_listening()
    sup.stop_listening()
    sup.start_listening()
    assert client.calls == 2
    assert sup.paused is False


# --- tick / watchdog ---

def test_tick_does_not_relaunch_while_paused():
    client = Spawner()
    sup = _make(Port(open_=True), client=client)
    sup.start_listening()
    sup.stop_listening()
    sup.tick()
    assert client.calls == 1  # no relaunch while paused


def test_tick_relaunches_client_after_unexpected_exit():
    client = Spawner()
    sup = _make(Port(open_=True), client=client)
    sup.start_listening()
    client.last.die()  # crashed
    sup.tick()
    assert client.calls == 2
    assert sup.state == "listening"


def test_tick_reports_brain_down_when_port_closed():
    client = Spawner()
    sup = _make(Port(open_=False), client=client)
    sup.tick()
    assert sup.state == "brain_down"
    assert client.calls == 0


def test_tick_stops_relaunching_after_max_failures():
    client = Spawner(alive=False)  # every spawned client is already dead
    sup = _make(Port(open_=True), client=client, max_relaunch=3)
    sup.start_listening()          # spawn #1 (immediately dead)
    for _ in range(6):
        sup.tick()
    assert client.calls <= 1 + 3   # initial + at most max_relaunch retries
    assert sup.state == "error"


# --- quit ---

def test_quit_stops_client_and_daemon_it_started():
    daemon = Spawner()
    client = Spawner()
    sup = _make(Port(seq=[False, True]), daemon=daemon, client=client)
    sup.ensure_daemon()            # started the daemon ourselves
    sup.start_listening()
    sup.quit()
    assert client.last.terminated is True
    assert daemon.last.terminated is True
    assert sup.state == "stopped"


def test_quit_leaves_externally_started_daemon_running():
    daemon = Spawner()
    client = Spawner()
    sup = _make(Port(open_=True), daemon=daemon, client=client)
    sup.ensure_daemon()            # daemon already up -> we didn't start it
    sup.start_listening()
    sup.quit()
    assert client.last.terminated is True
    assert daemon.calls == 0       # never spawned, nothing to terminate
