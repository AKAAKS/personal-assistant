"""The installer edits ~/.claude/settings.json -- the user's own configuration.

That file is not in this repo and holds everything from `permissions` to
`mcpServers`. So the installer merges rather than overwrites, backs the file up
before touching it, is idempotent, and refuses outright rather than replacing a
config it could not parse. These tests are the only thing standing between a
bug here and someone's Claude Code setup.

`install` takes the path, so nothing here goes near the real settings file.
"""

import json

import pytest

from scripts import install_claude_hooks as installer

EVENTS = ("SessionStart", "UserPromptSubmit", "PermissionRequest",
          "PermissionDenied", "PostToolUse", "PostToolUseFailure",
          "Notification", "Stop", "StopFailure", "SubagentStop", "SessionEnd")

SCRIPT = r"C:\repo\scripts\claude_status_hook.py"


def _settings(tmp_path, data):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path


def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _commands(hooks_block):
    for entries in hooks_block.values():
        for entry in entries:
            for hook in entry.get("hooks", []):
                yield hook


def test_it_registers_all_six_events(tmp_path):
    path = _settings(tmp_path, {})
    installer.install(path, script_path=SCRIPT)
    assert set(_read(path)["hooks"]) == set(EVENTS)


def test_every_entry_is_async(tmp_path):
    """A synchronous hook would make JARVIS able to stall a Claude turn."""
    path = _settings(tmp_path, {})
    installer.install(path, script_path=SCRIPT)
    hooks = list(_commands(_read(path)["hooks"]))
    assert len(hooks) == len(EVENTS)
    assert all(h.get("async") is True for h in hooks)


def test_every_entry_is_a_command_hook_naming_the_script(tmp_path):
    path = _settings(tmp_path, {})
    installer.install(path, script_path=SCRIPT)
    for hook in _commands(_read(path)["hooks"]):
        assert hook["type"] == "command"
        assert "claude_status_hook.py" in hook["command"]


def test_it_preserves_unrelated_settings_keys(tmp_path):
    original = {
        "model": "opus",
        "permissions": {"allow": ["Bash(git status)"]},
        "mcpServers": {"thing": {"command": "node"}},
        "enabledPlugins": ["superpowers@x"],
    }
    path = _settings(tmp_path, dict(original))
    installer.install(path, script_path=SCRIPT)
    after = _read(path)
    for key, value in original.items():
        assert after[key] == value


def test_it_merges_into_an_existing_hooks_block(tmp_path):
    existing = {"hooks": {"PreToolUse": [
        {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo hi"}]}
    ]}}
    path = _settings(tmp_path, existing)
    installer.install(path, script_path=SCRIPT)
    hooks = _read(path)["hooks"]
    assert hooks["PreToolUse"] == existing["hooks"]["PreToolUse"]
    assert set(EVENTS) <= set(hooks)


def test_it_keeps_someone_elses_hook_on_an_event_we_also_use(tmp_path):
    existing = {"hooks": {"Stop": [
        {"hooks": [{"type": "command", "command": "echo someone-elses-stop-hook"}]}
    ]}}
    path = _settings(tmp_path, existing)
    installer.install(path, script_path=SCRIPT)
    commands = [h["command"] for h in _commands(_read(path)["hooks"])]
    assert "echo someone-elses-stop-hook" in commands
    assert any("claude_status_hook.py" in c for c in commands)


def test_running_twice_does_not_duplicate_entries(tmp_path):
    path = _settings(tmp_path, {})
    installer.install(path, script_path=SCRIPT)
    installer.install(path, script_path=SCRIPT)
    commands = [h["command"] for h in _commands(_read(path)["hooks"])]
    ours = [c for c in commands if "claude_status_hook.py" in c]
    assert len(ours) == len(EVENTS)


def test_a_second_run_reports_that_nothing_changed(tmp_path):
    path = _settings(tmp_path, {})
    installer.install(path, script_path=SCRIPT)
    summary = installer.install(path, script_path=SCRIPT)
    assert "already" in summary.lower()


def test_it_writes_a_backup_first(tmp_path):
    original = {"model": "opus"}
    path = _settings(tmp_path, original)
    installer.install(path, script_path=SCRIPT)
    backups = list(tmp_path.glob("settings.json.bak*"))
    assert len(backups) == 1
    assert json.loads(backups[0].read_text(encoding="utf-8")) == original


def test_backup_false_writes_no_backup(tmp_path):
    path = _settings(tmp_path, {})
    installer.install(path, script_path=SCRIPT, backup=False)
    assert list(tmp_path.glob("settings.json.bak*")) == []


def test_a_missing_settings_file_is_created(tmp_path):
    path = tmp_path / "settings.json"
    installer.install(path, script_path=SCRIPT)
    assert set(_read(path)["hooks"]) == set(EVENTS)


def test_it_refuses_rather_than_clobbering_unparseable_settings(tmp_path):
    """Better to stop than to replace a config we could not read."""
    path = tmp_path / "settings.json"
    path.write_text('{"model": "opus", oops', encoding="utf-8")
    with pytest.raises(ValueError):
        installer.install(path, script_path=SCRIPT)
    assert path.read_text(encoding="utf-8") == '{"model": "opus", oops'


def test_it_refuses_a_hooks_value_that_is_not_an_object(tmp_path):
    """The docstring promises "merge, never overwrite" and "refuse rather than
    clobber". Silently replacing a hooks value we could not understand is
    neither."""
    path = _settings(tmp_path, {"model": "opus", "hooks": "oops"})
    with pytest.raises(ValueError):
        installer.install(path, script_path=SCRIPT)
    assert _read(path)["hooks"] == "oops"


def test_it_refuses_settings_that_are_not_an_object(tmp_path):
    path = _settings(tmp_path, [1, 2, 3])
    with pytest.raises(ValueError):
        installer.install(path, script_path=SCRIPT)


def test_the_script_path_is_absolute(tmp_path):
    entries = installer.hook_entries("scripts/claude_status_hook.py")
    for hook in _commands(entries):
        quoted = hook["command"].split('" "')[-1].rstrip('"')
        assert quoted != "scripts/claude_status_hook.py"
        assert "claude_status_hook.py" in quoted
        assert quoted.replace("\\", "/").startswith("/") or ":" in quoted


def test_the_summary_names_the_file_and_the_events(tmp_path):
    path = _settings(tmp_path, {})
    summary = installer.install(path, script_path=SCRIPT)
    assert "settings.json" in summary
    assert all(event in summary for event in EVENTS)


def test_a_moved_script_path_is_updated_not_duplicated(tmp_path):
    """The hook is registered by absolute path, so moving the repo -- or
    installing from a worktree and then from the merged checkout -- must
    re-point the existing entry rather than leave a dead one behind."""
    path = _settings(tmp_path, {})
    installer.install(path, script_path=r"C:\old\scripts\claude_status_hook.py")
    installer.install(path, script_path=r"C:\new\scripts\claude_status_hook.py")
    commands = [h["command"] for h in _commands(_read(path)["hooks"])]
    assert all("old" not in c for c in commands)
    assert len([c for c in commands if "new" in c]) == len(EVENTS)


def test_a_moved_script_path_is_reported(tmp_path):
    path = _settings(tmp_path, {})
    installer.install(path, script_path=r"C:\old\scripts\claude_status_hook.py")
    summary = installer.install(path,
                                script_path=r"C:\new\scripts\claude_status_hook.py")
    assert "updated" in summary.lower()


def test_it_creates_the_state_directory_the_hook_will_write_to(tmp_path):
    """The tool reads a MISSING state directory as "the hooks aren't installed
    yet". The directory is otherwise created by the first hook write, so
    installing and then asking before starting a session told the user to go
    install the thing they had just installed."""
    path = _settings(tmp_path, {})
    script = tmp_path / "repo" / "scripts" / "claude_status_hook.py"
    installer.install(path, script_path=str(script))
    assert (tmp_path / "repo" / "data" / "claude_sessions").is_dir()


def test_the_state_directory_can_be_overridden(tmp_path):
    path = _settings(tmp_path, {})
    state = tmp_path / "elsewhere"
    installer.install(path, script_path=SCRIPT, state_dir=state)
    assert state.is_dir()


def test_a_state_directory_that_cannot_be_created_does_not_fail_the_install(tmp_path):
    path = _settings(tmp_path, {})
    installer.install(path, script_path=SCRIPT,
                      state_dir=tmp_path / "settings.json" / "nope")
    assert set(_read(path)["hooks"]) == set(EVENTS)


def test_the_settings_file_stays_readable_json_with_indentation(tmp_path):
    path = _settings(tmp_path, {"model": "opus"})
    installer.install(path, script_path=SCRIPT)
    text = path.read_text(encoding="utf-8")
    assert "\n  " in text  # not collapsed onto one line
    json.loads(text)
