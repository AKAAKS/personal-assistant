"""The session record store: one small JSON file per Claude Code session.

These tests never need a Claude Code process. They feed `apply_event` the
payload shapes Claude Code puts on a hook's stdin and assert the record on
disk, which is the whole contract between the hook and the daemon.
"""

import json

from jarvisd import claude_sessions as cs


def _start(directory, session_id="s1", cwd=r"C:\p\personal-assistant", now=1000.0):
    cs.apply_event(
        {"hook_event_name": "SessionStart", "session_id": session_id, "cwd": cwd},
        directory=directory,
        now=lambda: now,
    )


def _stop(directory, message, session_id="s1", now=1100.0):
    cs.apply_event(
        {
            "hook_event_name": "Stop",
            "session_id": session_id,
            "last_assistant_message": message,
        },
        directory=directory,
        now=lambda: now,
    )


def test_session_start_creates_a_working_record(tmp_path):
    cs.apply_event(
        {
            "hook_event_name": "SessionStart",
            "session_id": "s1",
            "cwd": r"C:\p\personal-assistant",
        },
        directory=tmp_path,
    )
    rec = cs.read_raw(directory=tmp_path)[0]
    assert rec["state"] == cs.WORKING
    assert rec["project"] == "personal-assistant"
    assert rec["session_id"] == "s1"


def test_stop_records_the_last_message_truncated(tmp_path):
    _start(tmp_path)
    cs.apply_event(
        {
            "hook_event_name": "Stop",
            "session_id": "s1",
            "last_assistant_message": "x" * 900,
        },
        directory=tmp_path,
    )
    rec = cs.read_raw(directory=tmp_path)[0]
    assert rec["state"] == cs.DONE
    assert len(rec["last_message"]) == cs.MESSAGE_CAP


def test_idle_notification_does_not_overwrite_done(tmp_path):
    """The first draft's bug: idle 60s after Stop turned a finished session
    into 'waiting on you' and destroyed the message it had just recorded."""
    _start(tmp_path)
    _stop(tmp_path, "pushed the commit")
    cs.apply_event(
        {
            "hook_event_name": "Notification",
            "session_id": "s1",
            "message": "Claude is waiting for your input",
        },
        directory=tmp_path,
    )
    rec = cs.read_raw(directory=tmp_path)[0]
    assert rec["state"] == cs.DONE
    assert rec["last_message"] == "pushed the commit"
    assert rec["idle_ts"] is not None


def test_a_permission_notification_moves_the_state_to_waiting(tmp_path):
    _start(tmp_path)
    cs.apply_event(
        {
            "hook_event_name": "Notification",
            "session_id": "s1",
            "message": "Claude needs your permission to use Bash",
        },
        directory=tmp_path,
    )
    assert cs.read_raw(directory=tmp_path)[0]["state"] == cs.WAITING


def test_permission_request_waits_and_names_the_tool(tmp_path):
    _start(tmp_path)
    cs.apply_event(
        {"hook_event_name": "PermissionRequest", "session_id": "s1",
         "tool_name": "Bash"},
        directory=tmp_path,
    )
    rec = cs.read_raw(directory=tmp_path)[0]
    assert rec["state"] == cs.WAITING
    assert rec["tool_name"] == "Bash"


def test_a_new_prompt_clears_the_previous_last_message(tmp_path):
    _start(tmp_path)
    _stop(tmp_path, "pushed the commit")
    cs.apply_event(
        {"hook_event_name": "UserPromptSubmit", "session_id": "s1"},
        directory=tmp_path,
    )
    rec = cs.read_raw(directory=tmp_path)[0]
    assert rec["state"] == cs.WORKING
    assert rec["last_message"] is None


def test_a_new_prompt_clears_the_tool_it_was_waiting_on(tmp_path):
    """A stale tool_name is a confident wrong answer: "needs your input to use
    Bash" when the prompt is about Edit."""
    _start(tmp_path)
    cs.apply_event({"hook_event_name": "PermissionRequest", "session_id": "s1",
                    "tool_name": "Bash"}, directory=tmp_path)
    cs.apply_event({"hook_event_name": "UserPromptSubmit", "session_id": "s1"},
                   directory=tmp_path)
    assert cs.read_raw(directory=tmp_path)[0]["tool_name"] is None


def test_finishing_clears_the_tool_it_was_waiting_on(tmp_path):
    _start(tmp_path)
    cs.apply_event({"hook_event_name": "PermissionRequest", "session_id": "s1",
                    "tool_name": "Bash"}, directory=tmp_path)
    _stop(tmp_path, "approved and done")
    assert cs.read_raw(directory=tmp_path)[0]["tool_name"] is None


def test_a_permission_notification_does_not_inherit_an_old_tool(tmp_path):
    """PermissionRequest is unconfirmed to fire, so the Notification path is
    the likely real one; it must not report the previous prompt's tool."""
    _start(tmp_path)
    cs.apply_event({"hook_event_name": "PermissionRequest", "session_id": "s1",
                    "tool_name": "Bash"}, directory=tmp_path)
    _stop(tmp_path, "done")
    cs.apply_event({"hook_event_name": "UserPromptSubmit", "session_id": "s1"},
                   directory=tmp_path)
    cs.apply_event({"hook_event_name": "Notification", "session_id": "s1",
                    "message": "Claude needs your permission to use Edit"},
                   directory=tmp_path)
    rec = cs.read_raw(directory=tmp_path)[0]
    assert rec["state"] == cs.WAITING
    assert rec["tool_name"] is None


def test_since_only_moves_when_the_state_changes(tmp_path):
    _start(tmp_path)
    cs.apply_event(
        {"hook_event_name": "UserPromptSubmit", "session_id": "s1"},
        directory=tmp_path,
        now=lambda: 2000.0,
    )
    first = cs.read_raw(directory=tmp_path)[0]["since"]
    cs.apply_event(
        {"hook_event_name": "UserPromptSubmit", "session_id": "s1"},
        directory=tmp_path,
        now=lambda: 3000.0,
    )
    second = cs.read_raw(directory=tmp_path)[0]
    assert second["since"] == first
    assert second["updated_ts"] == 3000.0


def test_session_end_deletes_the_record(tmp_path):
    _start(tmp_path)
    assert cs.read_raw(directory=tmp_path) != []
    cs.apply_event(
        {"hook_event_name": "SessionEnd", "session_id": "s1"},
        directory=tmp_path,
    )
    assert cs.read_raw(directory=tmp_path) == []


def test_session_end_for_an_unknown_session_is_harmless(tmp_path):
    cs.apply_event(
        {"hook_event_name": "SessionEnd", "session_id": "never-seen"},
        directory=tmp_path,
    )
    assert cs.read_raw(directory=tmp_path) == []


def test_an_unknown_event_writes_nothing(tmp_path):
    cs.apply_event(
        {"hook_event_name": "WorktreeCreate", "session_id": "s9"},
        directory=tmp_path,
    )
    assert cs.read_raw(directory=tmp_path) == []


def test_a_corrupt_record_is_skipped_not_raised(tmp_path):
    (tmp_path / "bad.json").write_text("{not json", encoding="utf-8")
    assert cs.read_raw(directory=tmp_path) == []


def test_a_corrupt_record_does_not_hide_a_good_one(tmp_path):
    (tmp_path / "bad.json").write_text("{not json", encoding="utf-8")
    _start(tmp_path)
    assert [r["session_id"] for r in cs.read_raw(directory=tmp_path)] == ["s1"]


def test_an_event_over_a_corrupt_record_starts_it_fresh(tmp_path):
    (tmp_path / "s1.json").write_text("{truncated", encoding="utf-8")
    cs.apply_event(
        {"hook_event_name": "UserPromptSubmit", "session_id": "s1"},
        directory=tmp_path,
        now=lambda: 4000.0,
    )
    rec = cs.read_raw(directory=tmp_path)[0]
    assert rec["state"] == cs.WORKING
    assert rec["since"] == 4000.0


def test_a_missing_payload_field_never_raises(tmp_path):
    cs.apply_event({"hook_event_name": "SessionStart"}, directory=tmp_path)


def test_a_record_with_no_session_id_is_not_written(tmp_path):
    """A record with no id is unaddressable: there is no key to update it
    under and no way to delete it on SessionEnd."""
    cs.apply_event({"hook_event_name": "SessionStart"}, directory=tmp_path)
    assert cs.read_raw(directory=tmp_path) == []


def test_a_session_id_cannot_escape_the_state_directory(tmp_path):
    cs.apply_event(
        {"hook_event_name": "SessionStart", "session_id": r"../../evil"},
        directory=tmp_path / "state",
    )
    assert not (tmp_path / "evil.json").exists()


def test_the_write_leaves_no_temp_file_behind(tmp_path):
    _start(tmp_path)
    _stop(tmp_path, "done")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["s1.json"]


def test_the_record_on_disk_is_valid_json(tmp_path):
    _start(tmp_path)
    assert json.loads((tmp_path / "s1.json").read_text(encoding="utf-8"))["state"] == cs.WORKING


def test_a_missing_state_directory_reads_as_empty(tmp_path):
    assert cs.read_raw(directory=tmp_path / "nope") == []


def test_the_state_directory_is_absolute():
    assert cs.DIR.is_absolute()


def test_the_state_directory_is_under_data():
    assert cs.DIR.parent.name == "data"
    assert cs.DIR.name == "claude_sessions"
