"""Terminal/PC-power tool handlers (Tier A + B)."""

import pytest

from jarvisd.tools.terminal import handlers


class FakeSystem:
    def __init__(self, run_result=(0, "ok", ""), windows=None):
        self.run_result = run_result
        self._windows = windows or []
        self.ran = []
        self.opened = []
        self.power_calls = []

    def run_command(self, command):
        self.ran.append(command)
        return self.run_result

    def open_terminal(self, command=None):
        self.opened.append(command)

    def list_windows(self):
        return list(self._windows)

    def power(self, action, grace_seconds=60):
        self.power_calls.append((action, grace_seconds))
        return f"{action} in {grace_seconds}s"


@pytest.fixture(autouse=True)
def _shell_enabled(monkeypatch):
    monkeypatch.setenv("JARVIS_ALLOW_SHELL", "1")


# --- run_command (Tier B) ---

def test_run_command_runs_and_reports_output():
    sys_ = FakeSystem(run_result=(0, "On branch master", ""))
    out = handlers(sys_)["run_command"]({"command": "git status"})
    assert sys_.ran == ["git status"]
    assert "On branch master" in out and "0" in out  # output + exit code


def test_run_command_blocks_dangerous_without_running():
    sys_ = FakeSystem()
    out = handlers(sys_)["run_command"]({"command": "format c:"})
    assert sys_.ran == []                 # never reached the executor
    assert "blocked" in out.lower()


def test_run_command_respects_kill_switch(monkeypatch):
    monkeypatch.setenv("JARVIS_ALLOW_SHELL", "0")
    sys_ = FakeSystem()
    out = handlers(sys_)["run_command"]({"command": "git status"})
    assert sys_.ran == []
    assert "disabled" in out.lower()


# --- open_terminal (Tier A) ---

def test_open_terminal_launches_with_command():
    sys_ = FakeSystem()
    out = handlers(sys_)["open_terminal"]({"command": "claude"})
    assert sys_.opened == ["claude"]
    assert "claude" in out


def test_open_terminal_blocks_dangerous():
    sys_ = FakeSystem()
    out = handlers(sys_)["open_terminal"]({"command": "del /s /q C:\\*"})
    assert sys_.opened == []
    assert "blocked" in out.lower()


# --- scan_unsaved_work (read-only) ---

def test_scan_unsaved_work_flags_editors_and_dirty_titles():
    sys_ = FakeSystem(windows=[
        (10, "*Untitled - Notepad"),
        (11, "\u25cf main.py - Visual Studio Code"),
        (12, "Inbox - Google Chrome"),   # not an at-risk editor
    ])
    out = handlers(sys_)["scan_unsaved_work"]({})
    assert "Notepad" in out and "Visual Studio Code" in out
    assert "Chrome" not in out


def test_scan_unsaved_work_none():
    out = handlers(FakeSystem(windows=[(1, "Inbox - Google Chrome")]))["scan_unsaved_work"]({})
    assert "no" in out.lower()


# --- system_power (destructive; guard confirms) ---

def test_system_power_restart():
    sys_ = FakeSystem()
    out = handlers(sys_)["system_power"]({"action": "restart"})
    assert sys_.power_calls and sys_.power_calls[0][0] == "restart"


def test_system_power_cancel():
    sys_ = FakeSystem()
    handlers(sys_)["system_power"]({"action": "cancel"})
    assert sys_.power_calls[0][0] == "cancel"


def test_system_power_rejects_unknown_action():
    sys_ = FakeSystem()
    out = handlers(sys_)["system_power"]({"action": "explode"})
    assert sys_.power_calls == []
    assert "error" in out.lower()
