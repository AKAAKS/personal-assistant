"""launch_app must report failure honestly and resolve apps like the Run dialog."""

import os

from system_client import SystemClient


def test_launch_app_reports_failure_honestly(monkeypatch):
    def fake_startfile(cmd):
        raise FileNotFoundError(cmd)

    monkeypatch.setattr(os, "startfile", fake_startfile)
    out = SystemClient().launch_app("fortnite launcher 3000")
    assert "Could not find" in out
    assert "fortnite launcher 3000" in out


def test_launch_app_retries_with_exe_suffix(monkeypatch):
    calls = []

    def fake_startfile(cmd):
        calls.append(cmd)
        if not cmd.endswith(".exe"):
            raise FileNotFoundError(cmd)

    # firefox is in _RUNNING_NAMES, so launch_app would first probe the real
    # tasklist; pin it to "not running" so the test doesn't depend on whether
    # Firefox happens to be open on the machine running the suite.
    monkeypatch.setattr(SystemClient, "_is_running", lambda self, proc: False)
    monkeypatch.setattr(os, "startfile", fake_startfile)
    out = SystemClient().launch_app("firefox")
    assert out == "Launched firefox."
    assert calls == ["firefox", "firefox.exe"]


def test_launch_app_reports_already_running_without_relaunching(monkeypatch):
    calls = []
    monkeypatch.setattr(SystemClient, "_is_running", lambda self, proc: True)
    monkeypatch.setattr(os, "startfile", lambda cmd: calls.append(cmd))
    out = SystemClient().launch_app("firefox")
    assert out == "firefox is already running."
    assert calls == []  # source of truth beats memory: no relaunch


def test_launch_app_uses_protocol_for_epic(monkeypatch):
    calls = []
    monkeypatch.setattr(os, "startfile", lambda cmd: calls.append(cmd))
    out = SystemClient().launch_app("Epic Games")
    assert out == "Launched Epic Games."
    assert calls == ["com.epicgames.launcher:"]
