"""close_app must never take down the Windows shell.

Regression: "close File Explorer" ran `taskkill /IM explorer.exe /F`. All
File Explorer folder windows share the shell process by default, so the
force-kill took the desktop, taskbar, Start menu and tray with them —
a black screen with only Alt-Tab to navigate by.
"""

import os
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


# --- the escalation close_app advertises must actually be reachable ---------
# PostMessage(WM_CLOSE) is asynchronous and succeeds against a FROZEN window,
# so counting posted messages proved nothing. close_app claimed "Closed
# spotify." for a hung app, and the model -- told to use force_close_app only
# after close_app fails -- never escalated. Found in review of PR #9; the
# original failure-branch test stubbed the return code, so it never ran the
# real path.

def _no_terminate(monkeypatch):
    """close_app must decide from the windows, never fall through to a kill."""
    monkeypatch.setattr(SystemClient, "_terminate",
                        lambda self, p, l, force: pytest.fail("fell through to _terminate"))


def test_close_app_confirms_success_only_once_the_windows_are_gone(monkeypatch):
    monkeypatch.setattr(system_client, "_CLOSE_GRACE_SEC", 1.0)
    monkeypatch.setattr(system_client, "_CLOSE_POLL_SEC", 0.01)
    monkeypatch.setattr(SystemClient, "_close_windows", lambda self, proc: 1)
    seen = iter([[5150], []])  # still there, then gone
    monkeypatch.setattr(SystemClient, "_app_windows", lambda self, proc: next(seen, []))
    _no_terminate(monkeypatch)

    out = SystemClient().close_app("spotify")

    assert out == "Closed spotify.", out


def test_close_app_does_not_claim_success_for_a_hung_window(monkeypatch):
    monkeypatch.setattr(system_client, "_CLOSE_GRACE_SEC", 0.05)
    monkeypatch.setattr(system_client, "_CLOSE_POLL_SEC", 0.01)
    monkeypatch.setattr(SystemClient, "_close_windows", lambda self, proc: 1)
    monkeypatch.setattr(SystemClient, "_app_windows", lambda self, proc: [5150])
    monkeypatch.setattr(SystemClient, "_window_responds", lambda self, hwnd: False)
    _no_terminate(monkeypatch)

    out = SystemClient().close_app("spotify")

    assert "closed spotify." not in out.lower(), f"claimed success on a hung app: {out}"
    assert "force close" in out.lower(), out


def test_close_app_reports_a_save_prompt_instead_of_suggesting_force(monkeypatch):
    # A window that stays open but ANSWERS is showing "save your changes?".
    # Suggesting a force-kill here would destroy the work the dialog is
    # protecting, so this case must read differently from a hang.
    monkeypatch.setattr(system_client, "_CLOSE_GRACE_SEC", 0.05)
    monkeypatch.setattr(system_client, "_CLOSE_POLL_SEC", 0.01)
    monkeypatch.setattr(SystemClient, "_close_windows", lambda self, proc: 1)
    monkeypatch.setattr(SystemClient, "_app_windows", lambda self, proc: [5150])
    monkeypatch.setattr(SystemClient, "_window_responds", lambda self, hwnd: True)
    _no_terminate(monkeypatch)

    out = SystemClient().close_app("word")

    assert "unsaved" in out.lower(), out
    assert "force close" not in out.lower(), f"invited a force-kill over a save dialog: {out}"


# --- the self-guard must cover the WINDOW path too, not just _terminate -----
# _close_windows enumerated every window of the image and never subtracted
# _own_pids(), so close_app("pythonw") would WM_CLOSE JARVIS's own windows.
# Invisible to the suite because the self-protection test stubbed
# _close_windows to 0.

def test_app_windows_never_includes_jarvis_own_windows(monkeypatch):
    monkeypatch.setattr(SystemClient, "_pids_for_image", lambda self, proc: {os.getpid()})

    assert SystemClient()._app_windows("pythonw.exe") == []


def test_close_app_does_not_wm_close_jarvis_own_windows(monkeypatch):
    monkeypatch.setattr(SystemClient, "_pids_for_image", lambda self, proc: {os.getpid()})
    calls = _record_run(monkeypatch)

    out = SystemClient().close_app("pythonw")

    assert _taskkills(calls) == []
    assert "myself" in out.lower() or "jarvis" in out.lower(), out
