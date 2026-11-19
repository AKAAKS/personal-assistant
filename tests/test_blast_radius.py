"""Actions that sound routine but hit a shared system resource.

Sibling cases to the File Explorer bug, where `close_app("explorer")` ran
`taskkill /IM explorer.exe /F` and took the whole Windows shell down. Each
test below is the same shape: a loosely-resolved identifier reaching a
resource far bigger than the spoken request implied.
"""

import os
import subprocess
import types

import pytest

from security.command_sanitizer import sanitize_command
from system_client import SystemClient


def _record_run(monkeypatch, returncode=0, stdout="", stderr=""):
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


def _taskkills(calls):
    return [c for c in calls if isinstance(c, list) and c and c[0] == "taskkill"]


# --- 1. JARVIS must not kill JARVIS ----------------------------------------
# The daemon, the voice client and the tray all run as pythonw.exe
# (clients/tray_app.py spawns them with _pythonw()). Killing that image name
# is the explorer bug again, aimed at the assistant's own process.

def test_close_app_refuses_to_terminate_jarvis_itself(monkeypatch):
    monkeypatch.setattr(SystemClient, "_close_windows", lambda self, proc: 0)
    monkeypatch.setattr(SystemClient, "_pids_for_image", lambda self, proc: {os.getpid()})
    calls = _record_run(monkeypatch)

    out = SystemClient().close_app("pythonw")

    assert _taskkills(calls) == [], f"JARVIS tried to kill itself: {calls!r}"
    assert "myself" in out.lower() or "jarvis" in out.lower(), out


def test_force_close_app_refuses_to_terminate_jarvis_itself(monkeypatch):
    monkeypatch.setattr(SystemClient, "_pids_for_image", lambda self, proc: {os.getpid()})
    calls = _record_run(monkeypatch)

    out = SystemClient().force_close_app("pythonw")

    assert _taskkills(calls) == [], f"JARVIS force-killed itself: {calls!r}"
    assert "myself" in out.lower() or "jarvis" in out.lower(), out


def test_close_app_kills_other_processes_sharing_our_image_name(monkeypatch):
    # An unrelated pythonw app is still closable — only OUR pids are spared,
    # so the guard protects the assistant without disabling the feature.
    monkeypatch.setattr(SystemClient, "_close_windows", lambda self, proc: 0)
    monkeypatch.setattr(SystemClient, "_pids_for_image",
                        lambda self, proc: {os.getpid(), 424242})
    calls = _record_run(monkeypatch)

    SystemClient().close_app("pythonw")

    kills = _taskkills(calls)
    assert kills, "an unrelated process sharing the image name should close"
    flat = " ".join(" ".join(k) for k in kills)
    assert "424242" in flat, f"expected the other pid to be closed: {kills!r}"
    assert str(os.getpid()) not in flat, f"our own pid was targeted: {kills!r}"


def test_close_app_targets_pids_not_image_names(monkeypatch):
    # /IM hits every process sharing the name — the explorer mistake. By PID
    # the blast radius is exactly what was asked for.
    monkeypatch.setattr(SystemClient, "_close_windows", lambda self, proc: 0)
    monkeypatch.setattr(SystemClient, "_pids_for_image", lambda self, proc: {5150})
    calls = _record_run(monkeypatch)

    SystemClient().close_app("spotify")

    for args in _taskkills(calls):
        assert "/IM" not in args, f"closed by image name, not pid: {args!r}"


# --- 2. the same kill is still reachable through run_command ---------------
# The sanitizer blocks `shutdown` but not `taskkill`, so the exact command
# that blanked the screen can still arrive via the other door.

@pytest.mark.parametrize("cmd", [
    "taskkill /IM explorer.exe /F",
    "taskkill /f /im explorer.exe",
    "taskkill /IM dwm.exe /F",
    "Stop-Process -Name explorer -Force",
    "Stop-Process -Name lsass",
    "stop-process -name winlogon -force",
])
def test_sanitizer_blocks_killing_the_windows_shell(cmd):
    allowed, reason = sanitize_command(cmd)
    assert not allowed, f"sanitizer let the shell-killer through: {cmd}"
    assert "shell" in reason.lower() or "desktop" in reason.lower(), reason


@pytest.mark.parametrize("cmd", [
    "taskkill /IM notepad.exe",
    "Stop-Process -Name notepad",
    "taskkill /IM chrome.exe /F",
])
def test_sanitizer_still_allows_killing_ordinary_apps(cmd):
    allowed, _ = sanitize_command(cmd)
    assert allowed, f"ordinary app kill should still work: {cmd}"


# --- 3. open_url_browser is an unguarded ShellExecute ----------------------
# os.startfile runs whatever it is given. The tool says "URL", sits at the
# `write` tier, and so becomes arbitrary execution that skips run_command's
# confirmation and sanitizer entirely.

@pytest.mark.parametrize("target", [
    r"C:\Users\akshi\Downloads\installer.exe",
    r"C:\Windows\System32\cmd.exe",
    "file:///C:/Windows/System32/cmd.exe",
    "payload.bat",
])
def test_open_url_refuses_anything_that_is_not_a_web_url(monkeypatch, target):
    started = []
    monkeypatch.setattr(os, "startfile", lambda t: started.append(t))

    out = SystemClient().open_url(target)

    assert started == [], f"open_url executed a local target: {started!r}"
    assert "url" in out.lower(), out


@pytest.mark.parametrize("url", [
    "https://example.com",
    "http://localhost:8123",
    "mailto:someone@example.com",
])
def test_open_url_still_opens_real_urls(monkeypatch, url):
    started = []
    monkeypatch.setattr(os, "startfile", lambda t: started.append(t))

    SystemClient().open_url(url)

    assert started == [url]


# --- 4. Home Assistant's `all` wildcard ------------------------------------
# entity_id goes straight to call_service. HA treats "all" as every entity in
# the domain, so "turn off the lights" can mean the whole house — and the
# handler reports success without ever reading HA's response.

class FakeHA:
    def __init__(self):
        self.calls = []

    def call_service(self, domain, service, entity_id, data=None):
        self.calls.append((domain, service, entity_id, data))


def _ha_handlers(ha):
    from jarvisd.tools.ha import handlers
    return handlers(ha)


@pytest.mark.parametrize("entity_id", ["all", "ALL", "light", "light.", "none"])
def test_control_light_refuses_wildcard_targets(entity_id):
    ha = FakeHA()
    out = _ha_handlers(ha)["control_light"](
        {"entity_id": entity_id, "action": "turn_off"}
    )
    assert ha.calls == [], f"house-wide call made for {entity_id!r}: {ha.calls!r}"
    assert "entity" in out.lower(), out


def test_control_light_refuses_an_entity_from_another_domain():
    ha = FakeHA()
    out = _ha_handlers(ha)["control_light"](
        {"entity_id": "switch.water_heater", "action": "turn_off"}
    )
    assert ha.calls == [], f"light tool drove a switch: {ha.calls!r}"
    assert "light" in out.lower(), out


def test_control_light_still_controls_one_light():
    ha = FakeHA()
    out = _ha_handlers(ha)["control_light"](
        {"entity_id": "light.bedroom", "action": "turn_off"}
    )
    assert ha.calls == [("light", "turn_off", "light.bedroom", None)]
    assert "light.bedroom" in out


def test_control_switch_refuses_wildcard_targets():
    ha = FakeHA()
    out = _ha_handlers(ha)["control_switch"]({"entity_id": "all", "action": "turn_off"})
    assert ha.calls == []
    assert "entity" in out.lower(), out


def test_control_media_refuses_wildcard_targets():
    ha = FakeHA()
    out = _ha_handlers(ha)["control_media"]({"entity_id": "all", "action": "media_pause"})
    assert ha.calls == []
    assert "entity" in out.lower(), out


# --- 1b. the JARVIS family includes siblings, not just the line of descent --
# tray_app spawns the daemon AND the voice client as separate children. From
# the daemon, the voice client is a SIBLING: not an ancestor, not a child. A
# guard that only walked parents and children would still kill it.

class FakeProc:
    """Minimal psutil.Process stand-in for the family walk."""

    def __init__(self, pid, name, parent=None):
        self.pid = pid
        self._name = name
        self._parent = parent
        self._children = []
        if parent is not None:
            parent._children.append(self)

    def name(self):
        return self._name

    def parents(self):
        out, p = [], self._parent
        while p is not None:
            out.append(p)
            p = p._parent
        return out

    def children(self, recursive=False):
        if not recursive:
            return list(self._children)
        out = []
        for c in self._children:
            out.append(c)
            out.extend(c.children(recursive=True))
        return out


def _jarvis_tree():
    """explorer -> tray(pythonw) -> {daemon(pythonw) -> voice(pythonw)}"""
    shell = FakeProc(1, "explorer.exe")
    tray = FakeProc(10, "pythonw.exe", shell)
    daemon = FakeProc(11, "pythonw.exe", tray)
    voice = FakeProc(12, "pythonw.exe", tray)
    FakeProc(99, "notepad.exe", shell)  # unrelated app, must stay closable
    return shell, tray, daemon, voice


def test_process_family_from_the_daemon_includes_its_sibling_voice_client():
    from system_client import _process_family
    _, tray, daemon, voice = _jarvis_tree()

    family = _process_family(daemon)

    assert voice.pid in family, "the voice client is a sibling and was left exposed"
    assert tray.pid in family, "the tray spawned us and must be spared"
    assert daemon.pid in family


def test_process_family_stops_at_a_different_image_name():
    # Climbing past the tray into explorer.exe would adopt every unrelated
    # app as "family" and refuse to close anything.
    from system_client import _process_family
    shell, _, daemon, _ = _jarvis_tree()

    family = _process_family(daemon)

    assert shell.pid not in family, "walked up into the shell"
    assert 99 not in family, "an unrelated app was treated as JARVIS"
