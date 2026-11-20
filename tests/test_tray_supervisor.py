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


class Stopper:
    """Fake for the stop_daemon collaborator: records calls, optionally runs a hook
    (e.g. to flip the fake port closed, as a real kill would)."""

    def __init__(self, on_stop=None):
        self.calls = 0
        self.on_stop = on_stop

    def __call__(self):
        self.calls += 1
        if self.on_stop:
            self.on_stop()


def _make(port, daemon=None, client=None, stop=None, **kw):
    return JarvisSupervisor(
        is_port_open=port,
        spawn_daemon=daemon or Spawner(),
        spawn_client=client or Spawner(),
        stop_daemon=stop or Stopper(),
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


def test_tick_reports_brain_down_when_daemon_cannot_be_revived():
    client = Spawner()
    ticks = iter(range(0, 10_000, 10))
    sup = _make(Port(open_=False), client=client, daemon_timeout=5.0,
                clock=lambda: next(ticks))
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


# --- restart() stays client-only (regression pin) ---

def test_restart_restarts_only_the_client():
    daemon, client, stop = Spawner(), Spawner(), Stopper()
    sup = _make(Port(open_=True), daemon=daemon, client=client, stop=stop)
    sup.start_listening()
    sup.restart()
    assert client.calls == 2
    assert client.procs[0].terminated is True
    assert daemon.calls == 0
    assert stop.calls == 0
    assert sup.state == "listening"


# --- restart_daemon ---

def test_restart_daemon_stops_a_daemon_it_did_not_start():
    daemon, client, stop = Spawner(), Spawner(), Stopper()
    # closed after the kill, then the fresh daemon's port comes up
    sup = _make(Port(seq=[False, True]), daemon=daemon, client=client, stop=stop)
    assert sup.started_daemon is False
    assert sup.restart_daemon() is True
    assert stop.calls == 1
    assert daemon.calls == 1


def test_restart_daemon_waits_for_port_to_close_before_respawning():
    events = []
    port = Port(seq=[True, True, False, True])  # still open twice, then closed, then back
    real = port.__call__
    daemon = Spawner()

    def spawn():
        events.append("spawn")
        return daemon()

    sup = JarvisSupervisor(
        is_port_open=lambda: (events.append("probe"), real())[1],
        spawn_daemon=spawn, spawn_client=Spawner(),
        stop_daemon=lambda: events.append("stop"), sleep=lambda _s: None,
    )
    assert sup.restart_daemon() is True
    assert events.index("spawn") > events.index("stop")
    # the two "still open" probes happened before the spawn
    assert events[:events.index("spawn")].count("probe") == 3


def test_restart_daemon_restarts_client_and_owns_daemon():
    daemon, client = Spawner(), Spawner()
    sup = _make(Port(seq=[False, True]), daemon=daemon, client=client)
    sup.start_listening()
    old_client = client.last
    assert sup.restart_daemon() is True
    assert old_client.terminated is True
    assert client.calls == 2
    assert sup.started_daemon is True
    assert sup.state == "listening"
    sup.quit()
    assert daemon.last.terminated is True  # quit now stops the daemon we spawned


def test_restart_daemon_failing_to_come_back_is_not_listening():
    ticks = iter(range(0, 10_000, 10))
    client = Spawner()
    # closes after the kill, then never reopens
    sup = _make(Port(seq=[False], open_=False), client=client, daemon_timeout=5.0,
                clock=lambda: next(ticks))
    sup.start_listening()
    assert sup.restart_daemon() is False
    assert sup.state == "brain_down"
    assert client.calls == 1  # no new client against a dead brain


def test_restart_daemon_when_port_never_closes_does_not_respawn():
    ticks = iter(range(0, 10_000, 10))
    daemon = Spawner()
    sup = _make(Port(open_=True), daemon=daemon, daemon_timeout=5.0,
                clock=lambda: next(ticks))
    assert sup.restart_daemon() is False
    assert daemon.calls == 0
    assert sup.state == "brain_down"


def test_restart_daemon_keeps_a_deliberate_pause():
    """Pause is a deliberate mic release: restarting the brain must not re-arm it."""
    daemon, client, stop = Spawner(), Spawner(), Stopper()
    sup = _make(Port(seq=[False, True]), daemon=daemon, client=client, stop=stop)
    sup.start_listening()
    sup.stop_listening()
    assert client.calls == 1
    assert sup.restart_daemon() is True
    assert stop.calls == 1 and daemon.calls == 1   # daemon really restarted
    assert client.calls == 1                       # no voice client spawned
    assert sup.paused is True
    assert sup.state == "paused"
    sup.tick()                                     # watchdog must not re-arm it either
    assert client.calls == 1


# --- tick() self-heals a dead daemon ---

def test_tick_respawns_a_dead_daemon():
    daemon, client = Spawner(), Spawner()
    sup = _make(Port(seq=[False, True]), daemon=daemon, client=client)
    sup.tick()
    assert daemon.calls == 1
    assert sup.started_daemon is True
    assert sup.state == "listening"


def test_tick_does_not_respawn_daemon_while_paused():
    daemon = Spawner()
    sup = _make(Port(open_=False), daemon=daemon)
    sup.paused = True
    sup.tick()
    assert daemon.calls == 0


def test_daemon_cap_trips_to_error_and_stops_spawning():
    daemon = Spawner()
    ticks = iter(range(0, 100_000, 10))
    sup = _make(Port(open_=False), daemon=daemon, daemon_timeout=5.0,
                max_daemon_relaunch=2, clock=lambda: next(ticks))
    for _ in range(6):
        sup.tick()
    assert daemon.calls == 2
    assert sup.state == "error"


def test_daemon_cap_resets_after_recovery():
    port = Port(open_=False)
    daemon = Spawner()
    ticks = iter(range(0, 100_000, 10))
    healthy_on_spawn = {"on": False}

    def spawn():
        if healthy_on_spawn["on"]:
            port.open = True
        return daemon()

    sup = _make(port, daemon=spawn, client=Spawner(), daemon_timeout=5.0,
                max_daemon_relaunch=2, clock=lambda: next(ticks))
    sup.tick()                          # respawn #1 fails (port stays closed)
    assert sup._daemon_relaunch_count == 1
    healthy_on_spawn["on"] = True
    sup.tick()                          # respawn #2 succeeds
    assert sup._daemon_relaunch_count == 2
    sup.tick()                          # port open at start of tick: confirmed healthy
    assert sup._daemon_relaunch_count == 0
    port.open = False                   # dies again: allowed to respawn, not capped
    sup.tick()
    assert daemon.calls == 3
    assert sup.state != "error"


# --- lifecycle lock ---

def test_tick_skips_entirely_while_lifecycle_lock_held():
    daemon, client = Spawner(), Spawner()
    sup = _make(Port(open_=False), daemon=daemon, client=client)
    sup.state = "starting"
    sup._lifecycle.acquire()   # as if restart_daemon were mid-flight
    try:
        sup.tick()
    finally:
        sup._lifecycle.release()
    assert daemon.calls == 0
    assert client.calls == 0
    assert sup.state == "starting"   # untouched
    assert sup._daemon_relaunch_count == 0


def test_tick_releases_the_lock_even_when_it_raises():
    def boom():
        raise RuntimeError("probe failed")
    sup = _make(boom)
    try:
        sup.tick()
    except RuntimeError:
        pass
    assert sup._lifecycle.acquire(blocking=False)
    sup._lifecycle.release()


def test_restart_daemon_holds_the_lock_so_tick_cannot_race_it():
    daemon = Spawner()
    seen = {}
    holder = {}

    def stop():
        # a watchdog tick fires while the old daemon is being killed
        holder["sup"].tick()
        seen["daemon_calls_during_restart"] = daemon.calls

    sup = _make(Port(seq=[False, True]), daemon=daemon, stop=stop)
    holder["sup"] = sup
    sup.restart_daemon()
    assert seen["daemon_calls_during_restart"] == 0
    assert daemon.calls == 1   # exactly one daemon, the restart's own


# --- ERROR reason / tooltip ---

def test_daemon_cap_error_reason_and_label():
    from clients.tray_app import state_label
    ticks = iter(range(0, 100_000, 10))
    sup = _make(Port(open_=False), daemon_timeout=5.0, max_daemon_relaunch=1,
                clock=lambda: next(ticks))
    sup.tick()
    sup.tick()
    assert sup.state == "error" and sup.error_reason == "daemon"
    assert "brain" in state_label(sup.state, sup.error_reason)


def test_client_cap_error_reason_and_label():
    from clients.tray_app import state_label
    sup = _make(Port(open_=True), client=Spawner(alive=False), max_relaunch=1)
    sup.start_listening()
    for _ in range(4):
        sup.tick()
    assert sup.state == "error" and sup.error_reason == "client"
    assert "voice client" in state_label(sup.state, sup.error_reason)
    assert state_label(sup.state, sup.error_reason) != state_label(
        "error", "daemon")


# --- review fixes ---

def test_tick_respawn_does_not_rearm_mic_if_user_paused_meanwhile():
    port = Port(open_=False)
    client = Spawner()
    holder = {}
    daemon = Spawner()

    def spawn():
        port.open = True                 # the daemon comes up...
        holder["sup"].paused = True      # ...but the user hit Pause during the wait
        holder["sup"].state = "paused"
        return daemon()

    sup = _make(port, daemon=spawn, client=client)
    holder["sup"] = sup
    sup.tick()
    assert client.calls == 0
    assert sup.state == "paused"


def test_restart_daemon_survives_stop_daemon_raising():
    def deny():
        raise PermissionError("access denied")

    daemon, client = Spawner(), Spawner()
    sup = _make(Port(open_=True), daemon=daemon, client=client, stop=deny)
    sup.start_listening()
    assert sup.restart_daemon() is False       # no exception escapes
    assert sup.state == "brain_down"           # not stuck on STARTING
    assert daemon.calls == 0
    assert "access denied" in sup.last_error
    assert sup._lifecycle.acquire(blocking=False)  # lock released
    sup._lifecycle.release()


def test_restart_daemon_survives_spawn_daemon_raising():
    def boom():
        raise OSError("cannot spawn")

    sup = _make(Port(seq=[False]), daemon=boom)
    assert sup.restart_daemon() is False
    assert sup.state == "brain_down"


def test_tick_survives_spawn_daemon_raising():
    def boom():
        raise OSError("cannot spawn")

    sup = _make(Port(open_=False), daemon=boom)
    sup.tick()                                  # must not raise
    assert sup.state == "brain_down"


def test_quit_mid_restart_spawns_nothing_after_stopped():
    daemon, client = Spawner(), Spawner()
    holder = {}

    def stop():
        holder["sup"].quit()                    # user gets impatient mid-restart

    sup = _make(Port(seq=[False, True]), daemon=daemon, client=client, stop=stop)
    holder["sup"] = sup
    sup.start_listening()
    assert sup.restart_daemon() is False
    assert daemon.calls == 0
    assert client.calls == 1                    # only the original
    assert sup.state == "stopped"


def test_quit_during_daemon_spawn_terminates_the_new_daemon():
    holder = {}
    daemon = Spawner()

    def spawn():
        holder["sup"].quit()                    # lands while the spawn is in flight
        return daemon()

    sup = _make(Port(open_=False), daemon=spawn, client=Spawner())
    holder["sup"] = sup
    sup.tick()
    assert daemon.last.terminated is True       # no orphan holding the port
    assert sup.state == "stopped"


def test_start_listening_after_quit_spawns_nothing():
    client = Spawner()
    sup = _make(Port(open_=True), client=client)
    sup.quit()
    sup.start_listening()
    assert client.calls == 0
    assert sup.state == "stopped"


def test_tick_after_quit_spawns_nothing():
    client, daemon = Spawner(), Spawner()
    sup = _make(Port(open_=True), client=client, daemon=daemon)
    sup.quit()
    sup.tick()
    assert client.calls == 0 and daemon.calls == 0
    assert sup.state == "stopped"


def test_quit_during_client_spawn_in_start_listening_terminates_it():
    holder = {}
    client = Spawner()

    def spawn():
        holder["sup"].quit()          # lands inside the Popen window
        return client()

    sup = _make(Port(open_=True), client=spawn)
    holder["sup"] = sup
    sup.start_listening()
    assert client.last.terminated is True   # no live mic after quit
    assert sup._client is None
    assert sup.state == "stopped"


def test_quit_during_client_spawn_in_tick_terminates_it():
    holder = {}
    client = Spawner()

    def spawn():
        holder["sup"].quit()
        return client()

    sup = _make(Port(open_=True), client=spawn)
    holder["sup"] = sup
    sup.tick()
    assert client.last.terminated is True
    assert sup._client is None
    assert sup.state == "stopped"


def test_quit_during_port_wait_bails_instead_of_burning_the_timeout():
    holder = {}
    probes = []

    def probe():
        probes.append(1)
        holder["sup"].quit()           # quit lands while we poll for the port
        return False

    daemon = Spawner()
    sup = _make(probe, daemon=daemon, daemon_timeout=45.0)
    holder["sup"] = sup
    sup.tick()
    assert len(probes) <= 2            # not 45 sleeps' worth
    assert daemon.last is None or daemon.last.terminated is True
    assert sup.state == "stopped"


def test_pause_during_restart_daemon_tail_keeps_mic_off():
    holder = {}
    client = Spawner()
    daemon = Spawner()

    def spawn():
        holder["sup"].stop_listening()   # user pauses while the new daemon starts
        return daemon()

    sup = _make(Port(seq=[False, True]), daemon=spawn, client=client)
    holder["sup"] = sup
    assert sup.restart_daemon() is True
    assert client.calls == 0
    assert sup.state == "paused"


# --- startup path ---

def test_startup_starts_daemon_and_client():
    client = Spawner()
    sup = _make(Port(open_=True), client=client)
    sup.startup()
    assert client.calls == 1 and sup.state == "listening"


def test_startup_respects_pause_pressed_while_daemon_comes_up():
    holder = {}
    port = Port(open_=False)
    client = Spawner()
    daemon = Spawner()

    def spawn():
        port.open = True
        holder["sup"].stop_listening()   # menu is live during ensure_daemon
        return daemon()

    sup = _make(port, daemon=spawn, client=client)
    holder["sup"] = sup
    sup.startup()
    assert client.calls == 0
    assert sup.paused is True and sup.state == "paused"


def test_startup_skips_client_when_brain_down():
    ticks = iter(range(0, 10_000, 10))
    client = Spawner()
    sup = _make(Port(open_=False), client=client, daemon_timeout=5.0,
                clock=lambda: next(ticks))
    sup.startup()
    assert client.calls == 0 and sup.state == "brain_down"


# --- watchdog failure accounting ---

def test_watch_counts_consecutive_failures_to_error_and_logs():
    logged = []

    def boom():
        raise RuntimeError("probe exploded")

    sup = JarvisSupervisor(is_port_open=boom, spawn_daemon=Spawner(),
                           spawn_client=Spawner(), stop_daemon=Stopper(),
                           log_error=logged.append, sleep=lambda _s: None)
    sup.watch()
    sup.watch()
    assert sup.state != "error"
    sup.watch()
    assert sup.state == "error" and sup.error_reason == "watchdog"
    assert len(logged) == 3 and "probe exploded" in logged[0]
    assert "RuntimeError" in sup.last_error


def test_watch_resets_failure_counter_on_clean_tick():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] <= 2:
            raise RuntimeError("blip")
        return True

    sup = _make(flaky)
    sup.watch(); sup.watch()
    sup.watch()                      # clean
    assert sup._tick_failures == 0
    assert sup.state != "error"


# --- last_error visibility ---

def test_elevated_daemon_failure_shows_run_elevated_hint():
    from clients.tray_app import state_label

    def deny():
        raise PermissionError("access denied stopping daemon pid(s) [1234]")

    sup = _make(Port(open_=True), stop=deny)
    sup.restart_daemon()
    assert sup.state == "brain_down"
    label = state_label(sup.state, sup.error_reason, sup.last_error)
    assert "run elevated" in label
    # ...while a generic failure keeps the plain label
    assert "elevated" not in state_label("brain_down", None, "OSError('boom')")
    assert "elevated" not in state_label("brain_down", None, None)


def test_last_error_clears_after_a_good_spawn():
    def deny():
        raise PermissionError("access denied")

    state = {"deny": True}

    def stop():
        if state["deny"]:
            deny()

    sup = _make(Port(seq=[True, False, True], open_=True), stop=stop)
    assert sup.restart_daemon() is False
    assert sup.last_error
    state["deny"] = False
    assert sup.restart_daemon() is True
    assert sup.last_error is None


# --- _stop_port_owner: AccessDenied -> PermissionError ---

def test_stop_port_owner_converts_access_denied(monkeypatch):
    import types
    import psutil
    from clients import tray_app

    monkeypatch.setenv("JARVIS_PORT", "8765")
    conn = types.SimpleNamespace(status=psutil.CONN_LISTEN, pid=424242,
                                 laddr=types.SimpleNamespace(port=8765))
    monkeypatch.setattr(psutil, "net_connections", lambda kind="tcp": [conn])

    real_process = psutil.Process

    class FakeProcess(real_process):
        def __init__(self, pid=None):
            self._fake = pid == 424242
            if not self._fake:
                super().__init__(pid)

        def terminate(self):
            if self._fake:
                raise psutil.AccessDenied(424242)
            super().terminate()

    monkeypatch.setattr(psutil, "Process", FakeProcess)
    import pytest
    with pytest.raises(PermissionError, match="access denied"):
        tray_app._stop_port_owner()


def test_error_reason_clears_when_leaving_error():
    port = Port(open_=True)
    client = Spawner(alive=False)
    sup = _make(port, client=client, max_relaunch=1)
    sup.start_listening()
    for _ in range(3):
        sup.tick()
    assert sup.state == "error" and sup.error_reason == "client"
    sup.start_listening()                       # user recovers it
    assert sup.error_reason is None
