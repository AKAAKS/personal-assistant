"""launch_app must report failure honestly and resolve apps like the Run dialog."""

import os
import subprocess

from system_client import SystemClient


def _record_popen(monkeypatch):
    """Patch subprocess.Popen to record (args, kwargs) and return a dummy proc.

    The first Popen (`wt ...`) "succeeds" so open_terminal returns after one
    call — mirroring the happy path where Windows Terminal is present.
    """
    calls = []

    def fake_popen(args, **kwargs):
        calls.append((args, kwargs))
        return object()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    return calls


def test_open_terminal_scrubs_anthropic_credentials_when_launching_claude(monkeypatch):
    # JARVIS's daemon env carries the API key (loaded from .env). A terminal
    # that runs Claude Code must NOT inherit it, so Claude Code falls back to
    # the user's subscription login instead of billing the API.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "auth-tok")
    calls = _record_popen(monkeypatch)

    SystemClient().open_terminal("claude")

    assert calls, "expected open_terminal to spawn a process"
    args, kwargs = calls[0]
    # Must spawn cmd.exe DIRECTLY, never via `wt`: Windows Terminal's
    # single-instance model can defeat a per-invocation env, so the scrub
    # would not reliably reach Claude Code.
    assert args[0] == "cmd", f"claude must bypass wt for a reliable env, got {args!r}"
    env = kwargs.get("env")
    assert env is not None, "a claude terminal must get a scrubbed env, not inherit"
    assert "ANTHROPIC_API_KEY" not in env
    assert "ANTHROPIC_AUTH_TOKEN" not in env
    # The rest of the environment is preserved (PATH etc. still there).
    assert env.get("PATH") == os.environ.get("PATH")


def test_open_terminal_detects_claude_with_args_and_full_path(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
    for cmd in ("claude --resume", r'"C:\Program Files\claude\claude.exe" chat'):
        calls = _record_popen(monkeypatch)
        SystemClient().open_terminal(cmd)
        env = calls[0][1].get("env")
        assert env is not None and "ANTHROPIC_API_KEY" not in env, cmd


def test_open_terminal_preserves_env_for_non_claude_command(monkeypatch):
    # Day-to-day terminals (dev servers, git, etc.) keep Windows Terminal and
    # inherit the daemon environment unchanged — env=None means "inherit".
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
    calls = _record_popen(monkeypatch)

    SystemClient().open_terminal("npm run dev")

    assert calls[0][0][0] == "wt", "normal terminals keep the Windows Terminal path"
    assert calls[0][1].get("env") is None


def test_open_terminal_preserves_env_for_empty_terminal(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
    calls = _record_popen(monkeypatch)

    SystemClient().open_terminal()

    assert calls[0][0][0] == "wt"
    assert calls[0][1].get("env") is None


def test_open_terminal_does_not_false_match_claude_prefix(monkeypatch):
    # A command that merely starts with "claude" (but isn't the claude CLI)
    # must NOT trigger scrubbing.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
    calls = _record_popen(monkeypatch)

    SystemClient().open_terminal("claudexyz --flag")

    assert calls[0][1].get("env") is None


# --- run_command: a headless `claude ...` is still Claude Code, not JARVIS ---

def _record_run(monkeypatch):
    import types
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


def test_run_command_scrubs_anthropic_credentials_for_claude(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "auth-tok")
    calls = _record_run(monkeypatch)

    SystemClient().run_command('claude -p "summarize this"')

    env = calls[0][1].get("env")
    assert env is not None and "ANTHROPIC_API_KEY" not in env
    assert "ANTHROPIC_AUTH_TOKEN" not in env


def test_run_command_preserves_env_for_normal_command(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
    calls = _record_run(monkeypatch)

    SystemClient().run_command("git status")

    assert calls[0][1].get("env") is None


# --- launch_app: "open claude" must go through the scrubbed terminal path ---

def test_launch_app_routes_claude_through_scrubbed_terminal(monkeypatch):
    opened = []
    monkeypatch.setattr(SystemClient, "open_terminal", lambda self, cmd=None: opened.append(cmd))
    # os.startfile must NOT be used for claude (it can't scrub the env).
    monkeypatch.setattr(os, "startfile", lambda cmd: opened.append(("STARTFILE", cmd)))

    out = SystemClient().launch_app("claude")

    assert opened == ["claude"], f"claude must route to open_terminal, got {opened!r}"
    assert "terminal" in out.lower()


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
