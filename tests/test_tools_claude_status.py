"""The claude_status tool: what JARVIS says when asked "is Claude still going?"

The design's second anti-goal is confident wrong answers, so three answers have
to stay distinct: the hooks were never installed, nothing is running, and here
is what is running. The text is read aloud, so it also has to survive whatever
markdown a Claude reply happened to end in.
"""

from jarvisd import claude_sessions as cs
from jarvisd.core import CAPABILITIES
from jarvisd.tools import build_registry
from jarvisd.tools import dev_status as cs_tool
from security.policy import TOOL_PERMISSIONS

NOW = 10_000.0


def _call(*, directory, now=NOW):
    handler = cs_tool.handlers(directory=directory, now=lambda: now,
                              probe=lambda pid: 1.0)["claude_status"]
    return handler({})


def _write(directory, session_id, event, *, project="proj", age=60.0, **extra):
    directory.mkdir(parents=True, exist_ok=True)
    payload = {"hook_event_name": "SessionStart", "session_id": session_id,
               "cwd": f"/home/user/{project}"}
    cs.apply_event(payload, directory=directory, now=lambda: NOW - age - 1,
                   ancestor=lambda: (1, 1.0))
    if event:
        payload = {"hook_event_name": event, "session_id": session_id, **extra}
        cs.apply_event(payload, directory=directory, now=lambda: NOW - age,
                       ancestor=lambda: (1, 1.0))


def _working(directory, session_id="s1", *, project="proj", age=300.0):
    _write(directory, session_id, "UserPromptSubmit", project=project, age=age)


def _waiting(directory, session_id="s2", *, project="proj", age=120.0, tool="Bash"):
    _write(directory, session_id, "PermissionRequest", project=project, age=age,
           tool_name=tool)


def _done(directory, message, *, session_id="s3", project="proj", age=60.0):
    _write(directory, session_id, "Stop", project=project, age=age,
           last_assistant_message=message)


# --- the three distinct answers --------------------------------------------

def test_a_missing_state_dir_says_the_hooks_are_not_installed(tmp_path):
    out = _call(directory=tmp_path / "nope")
    assert "install" in out.lower()


def test_a_missing_state_dir_does_not_claim_nothing_is_running(tmp_path):
    """Saying "nothing is running" when the hooks were never wired is the
    confident wrong answer the design names."""
    assert "no claude code session" not in _call(directory=tmp_path / "nope").lower()


def test_an_empty_state_dir_says_nothing_is_running(tmp_path):
    assert "no claude code session" in _call(directory=tmp_path).lower()


def test_a_directory_of_only_dead_records_says_nothing_is_running(tmp_path):
    _working(tmp_path)
    handler = cs_tool.handlers(directory=tmp_path, now=lambda: NOW,
                               probe=lambda pid: None)["claude_status"]
    assert "no claude code session" in handler({}).lower()


# --- what a live session reads as -----------------------------------------

def test_a_working_session_names_the_project_and_the_duration(tmp_path):
    _working(tmp_path, age=300.0)
    out = _call(directory=tmp_path)
    assert "proj" in out
    assert "working" in out.lower()
    assert "5 minutes" in out


def test_a_waiting_session_says_it_needs_you(tmp_path):
    _waiting(tmp_path, age=120.0)
    out = _call(directory=tmp_path)
    assert "needs your input" in out.lower()
    assert "proj" in out


def test_a_waiting_session_names_the_tool_it_is_blocked_on(tmp_path):
    _waiting(tmp_path, tool="Bash")
    assert "Bash" in _call(directory=tmp_path)


def test_a_recently_finished_session_quotes_what_it_said(tmp_path):
    _done(tmp_path, "pushed the commit", age=60)
    assert "pushed the commit" in _call(directory=tmp_path)


def test_an_old_finished_session_is_counted_not_quoted(tmp_path):
    _done(tmp_path, "pushed the commit", age=cs_tool.DONE_REPORT_SEC + 60)
    out = _call(directory=tmp_path)
    assert "pushed the commit" not in out
    assert "finished" in out.lower()


def test_a_finished_session_with_no_message_still_reports(tmp_path):
    _done(tmp_path, None, age=60)
    out = _call(directory=tmp_path)
    assert "finished" in out.lower()
    assert "proj" in out


def test_two_sessions_in_the_same_project_are_both_reported(tmp_path):
    """Review Focus 4: same cwd must not merge or silently drop one."""
    _working(tmp_path, "s1", project="proj")
    _waiting(tmp_path, "s2", project="proj")
    out = _call(directory=tmp_path)
    assert out.count("proj") >= 2


def test_two_sessions_in_the_same_project_are_distinguishable(tmp_path):
    _working(tmp_path, "s1", project="proj")
    _waiting(tmp_path, "s2", project="proj")
    out = _call(directory=tmp_path)
    assert "session 1" in out.lower() and "session 2" in out.lower()


def test_one_session_is_not_numbered(tmp_path):
    _working(tmp_path, "s1", project="proj")
    assert "session 1" not in _call(directory=tmp_path).lower()


def test_sessions_in_different_projects_are_both_reported(tmp_path):
    _working(tmp_path, "s1", project="alpha")
    _waiting(tmp_path, "s2", project="beta")
    out = _call(directory=tmp_path)
    assert "alpha" in out and "beta" in out


# --- it is spoken, so it has to survive formatting ------------------------

def test_a_markdown_and_multiline_message_is_flattened(tmp_path):
    """Review Focus 5: this text is spoken, so it must survive formatting."""
    _done(tmp_path, "**bold**\n\n- a\n- b", age=10)
    out = _call(directory=tmp_path)
    assert "\n\n" not in out and "**" not in out


def test_a_non_ascii_message_survives(tmp_path):
    _done(tmp_path, "renamed café.py — done", age=10)
    assert "café.py" in _call(directory=tmp_path)


def test_a_corrupt_record_does_not_break_the_answer(tmp_path):
    """Review Focus 1: a hand-edited or half-written file is skipped."""
    _working(tmp_path, "s1", project="proj")
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
    assert "proj" in _call(directory=tmp_path)


# --- wiring ----------------------------------------------------------------

def test_the_tool_is_read_only_in_the_policy():
    assert TOOL_PERMISSIONS["claude_status"] == "read_only"


def test_the_tool_is_registered_unconditionally():
    tools, handlers = build_registry(ha_client=None, system_client=None)
    assert "claude_status" in handlers
    assert any(t["name"] == "claude_status" for t in tools)


def test_the_tool_takes_no_arguments():
    definition = next(t for t in cs_tool.TOOLS if t["name"] == "claude_status")
    assert definition["input_schema"]["properties"] == {}
    assert definition["input_schema"]["required"] == []


def test_the_description_is_about_claude_code_not_about_jarvis():
    definition = next(t for t in cs_tool.TOOLS if t["name"] == "claude_status")
    assert "Claude Code" in definition["description"]


def test_the_prompt_advertises_it():
    assert any("claude_status" in names for _, names in CAPABILITIES)
