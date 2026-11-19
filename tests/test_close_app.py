"""close_app must never take down the Windows shell.

Regression: "close File Explorer" ran `taskkill /IM explorer.exe /F`. All
File Explorer folder windows share the shell process by default, so the
force-kill took the desktop, taskbar, Start menu and tray with them —
a black screen with only Alt-Tab to navigate by.
"""

import subprocess
import types

import pytest

import system_client
from system_client import SystemClient


def _record_run(monkeypatch, returncode=0, stdout="", stderr=""):
    """Record every subprocess.run call close_app makes."""
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


def _taskkills(calls):
    return [c for c in calls if isinstance(c, list) and c and c[0] == "taskkill"]


# --- the regression itself -------------------------------------------------

@pytest.mark.parametrize("spoken", ["explorer", "file explorer", "File Explorer", "windows explorer"])
def test_close_app_never_force_kills_the_shell(monkeypatch, spoken):
    # No window closed gracefully -> the OLD code fell through to taskkill /F.
    monkeypatch.setattr(SystemClient, "_close_windows", lambda self, proc: 0)
    calls = _record_run(monkeypatch)

    SystemClient().close_app(spoken)

    for args in _taskkills(calls):
        assert "/F" not in args, f"force-killed the shell: {args!r}"
        assert "explorer.exe" not in args, f"taskkill aimed at the shell: {args!r}"


def test_close_app_refuses_the_shell_and_says_why(monkeypatch):
    monkeypatch.setattr(SystemClient, "_close_windows", lambda self, proc: 0)
    calls = _record_run(monkeypatch)

    out = SystemClient().close_app("file explorer")

    assert _taskkills(calls) == [], "the shell must never reach taskkill"
    assert "no" in out.lower() and "window" in out.lower(), out


def test_close_app_closes_explorer_folder_windows_without_touching_the_process(monkeypatch):
    closed = []
    monkeypatch.setattr(SystemClient, "_close_windows",
                        lambda self, proc: closed.append(proc) or 2)
    calls = _record_run(monkeypatch)

    out = SystemClient().close_app("file explorer")

    assert closed == ["explorer.exe"]
    assert _taskkills(calls) == [], "closing a window must not also kill the process"
    assert "closed" in out.lower(), out


# --- graceful-first for every other app ------------------------------------

def test_close_app_prefers_closing_windows_over_killing(monkeypatch):
    # An app that owns a window gets WM_CLOSE, so it can prompt to save —
    # the same reason power() omits shutdown's /f.
    monkeypatch.setattr(SystemClient, "_close_windows", lambda self, proc: 1)
    calls = _record_run(monkeypatch)

    out = SystemClient().close_app("word")

    assert _taskkills(calls) == [], "an app with a window must not be killed"
    assert "closed" in out.lower(), out


def test_close_app_falls_back_to_taskkill_without_force(monkeypatch):
    # Windowless/tray app: taskkill is allowed, but never with /F, and aimed
    # at a PID rather than an image name (see test_blast_radius.py).
    monkeypatch.setattr(SystemClient, "_close_windows", lambda self, proc: 0)
    monkeypatch.setattr(SystemClient, "_pids_for_image", lambda self, proc: {31337})
    calls = _record_run(monkeypatch)

    SystemClient().close_app("spotify")

    kills = _taskkills(calls)
    assert kills, "a windowless app should still be closable"
    assert "/F" not in kills[0], f"close_app must never force: {kills[0]!r}"
    assert "31337" in kills[0]


def test_close_app_suggests_force_when_the_app_will_not_exit(monkeypatch):
    monkeypatch.setattr(SystemClient, "_close_windows", lambda self, proc: 0)
    monkeypatch.setattr(SystemClient, "_pids_for_image", lambda self, proc: {31337})
    _record_run(monkeypatch, returncode=1, stderr="ERROR: could not be terminated")

    out = SystemClient().close_app("spotify")

    assert "force" in out.lower(), out


# --- the force path is explicit, and still refuses the shell ---------------

def test_force_close_app_uses_force_for_a_normal_app(monkeypatch):
    monkeypatch.setattr(SystemClient, "_pids_for_image", lambda self, proc: {31337})
    calls = _record_run(monkeypatch)

    SystemClient().force_close_app("spotify")

    kills = _taskkills(calls)
    assert kills and "/F" in kills[0] and "31337" in kills[0]


@pytest.mark.parametrize("proc", ["explorer", "winlogon", "csrss", "lsass", "dwm", "services"])
def test_force_close_app_refuses_protected_processes(monkeypatch, proc):
    calls = _record_run(monkeypatch)

    out = SystemClient().force_close_app(proc)

    assert _taskkills(calls) == [], f"{proc} must never be force-killed: {calls!r}"
    assert "refus" in out.lower() or "won't" in out.lower(), out


# --- the window filter that keeps the desktop and taskbar alive ------------

@pytest.mark.parametrize("cls", ["Progman", "Shell_TrayWnd", "Shell_SecondaryTrayWnd", "WorkerW"])
def test_shell_windows_are_not_closable(cls):
    assert system_client._is_shell_window(cls) is True


@pytest.mark.parametrize("cls", ["CabinetWClass", "ExploreWClass", "Chrome_WidgetWin_1", "Notepad"])
def test_ordinary_windows_are_closable(cls):
    assert system_client._is_shell_window(cls) is False
