from tests.test_tools_registry import FakeHA, FakeSystem


def make_assistant():
    from jarvisd.core import Assistant
    return Assistant(FakeHA(), "sk-test-not-a-real-key", system_client=FakeSystem())


def test_tools_built_from_registry():
    ai = make_assistant()
    names = [t["name"] for t in ai.tools]
    assert "list_devices" in names
    assert "spotify_play" not in names  # no spotify client passed


def test_execute_tool_dispatch():
    ai = make_assistant()
    assert "Desk" in ai._execute_tool("list_devices", {})
    assert ai._execute_tool("nope_tool", {}) == "Unknown tool: nope_tool"


def test_execute_tool_wraps_exceptions():
    ai = make_assistant()
    out = ai._execute_tool("control_light", {})  # missing required keys -> KeyError inside
    assert out.startswith("Error executing control_light")


def test_models_are_current():
    from jarvisd import core
    assert core.HAIKU_MODEL == "claude-haiku-4-5"
    assert core.SONNET_MODEL == "claude-sonnet-5"


def test_execute_guarded_autonomous_destructive_never_executes():
    """On the autonomous path (HA trigger rules) a destructive tool must stay
    CONFIRMATION REQUIRED forever -- the retry-confirm dance is only meaningful
    when a user is present to confirm."""
    from types import SimpleNamespace
    from security import policy

    ai = make_assistant()
    ai.audit = SimpleNamespace(log=lambda *a, **k: None)  # no logs/ writes from tests
    executed = []
    ai._handlers["fake_destructive"] = lambda ti: executed.append(ti) or "did it"
    policy.TOOL_PERMISSIONS["fake_destructive"] = "destructive"
    try:
        first = ai.execute_guarded("fake_destructive", {"a": 1}, autonomous=True)
        second = ai.execute_guarded("fake_destructive", {"a": 1}, autonomous=True)
        assert first.startswith("CONFIRMATION REQUIRED")
        assert second.startswith("CONFIRMATION REQUIRED")
        assert executed == []
    finally:
        del policy.TOOL_PERMISSIONS["fake_destructive"]


def test_execute_guarded_run_script_confirm_then_executes():
    """run_script is a real policy entry (permission "destructive", Task 6) --
    this exercises the full execute_guarded path end-to-end: guard verdict ->
    handler executed or not -> audit, with no monkeypatched fake permission."""
    from types import SimpleNamespace

    ai = make_assistant()
    ai.audit = SimpleNamespace(log=lambda *a, **k: None)  # no logs/ writes from tests
    executed = []
    ai._handlers["run_script"] = lambda ti: executed.append(ti) or "ran it"

    ai.guard.begin_turn()
    first = ai.execute_guarded("run_script", {"name": "x.py"})
    assert first.startswith("CONFIRMATION REQUIRED")
    assert executed == []

    # A same-turn retry (the model looping) must NOT auto-confirm.
    same_turn = ai.execute_guarded("run_script", {"name": "x.py"})
    assert same_turn.startswith("CONFIRMATION REQUIRED")
    assert executed == []

    ai.guard.begin_turn()  # user's next turn confirms
    second = ai.execute_guarded("run_script", {"name": "x.py"})
    assert second == "ran it"
    assert executed == [{"name": "x.py"}]


def test_execute_guarded_run_script_autonomous_never_executes():
    """Autonomous callers (trigger rules) must never get run_script executed
    via the retry-confirm dance -- it must stay terminally CONFIRM_REQUIRED."""
    from types import SimpleNamespace

    ai = make_assistant()
    ai.audit = SimpleNamespace(log=lambda *a, **k: None)  # no logs/ writes from tests
    executed = []
    ai._handlers["run_script"] = lambda ti: executed.append(ti) or "ran it"

    first = ai.execute_guarded("run_script", {"name": "x.py"}, autonomous=True)
    second = ai.execute_guarded("run_script", {"name": "x.py"}, autonomous=True)

    assert first.startswith("CONFIRMATION REQUIRED")
    assert second.startswith("CONFIRMATION REQUIRED")
    assert executed == []


def test_system_prompt_carries_spoken_persona_rules():
    # The persona rules are fixed text, so they live in the template rather than
    # in the per-run capability tables -- see render_system_prompt().
    from jarvisd.core import SYSTEM_PROMPT_TEMPLATE

    p = SYSTEM_PROMPT_TEMPLATE.lower()
    assert "no markdown" in p
    assert "no bullet" in p or "no lists" in p
    assert "read aloud" in p
    # never the butler honorific — check the actual prohibition, not just that "sir" appears
    assert 'never use "sir"' in SYSTEM_PROMPT_TEMPLATE


# --- Issue #3: _compact_history must not orphan a tool_result -------------

def _tool_turn(i):
    """One tool-using turn: user text, assistant tool_use, user tool_result,
    assistant text -- the 4-entry shape that makes the slice land badly."""
    return [
        {"role": "user", "content": f"turn {i}"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": f"t{i}", "name": "x", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"t{i}", "content": "ok"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
    ]


def _first_is_plain_user(conv):
    msg = conv[0]
    if msg["role"] != "user":
        return False
    c = msg["content"]
    if isinstance(c, str):
        return True
    return not any(
        (b.get("type") if isinstance(b, dict) else getattr(b, "type", None)) == "tool_result"
        for b in c
    )


def test_compact_history_drops_orphaned_tool_result():
    """The naive slice can start on a user message holding only tool_result
    blocks whose tool_use is gone -- the API rejects that with a 400."""
    ai = make_assistant()
    # 42 entries, arranged so the last-40 slice starts on the orphaned result.
    ai.conversation = []
    for i in range(12):
        ai.conversation.extend(_tool_turn(i))
    ai.conversation = ai.conversation[:42]
    assert len(ai.conversation) == 42

    ai._compact_history()

    assert len(ai.conversation) <= ai.MAX_HISTORY
    assert _first_is_plain_user(ai.conversation)
    ids = {
        b["id"]
        for m in ai.conversation
        if m["role"] == "assistant" and not isinstance(m["content"], str)
        for b in m["content"]
        if b.get("type") == "tool_use"
    }
    orphans = [
        b["tool_use_id"]
        for m in ai.conversation
        if m["role"] == "user" and not isinstance(m["content"], str)
        for b in m["content"]
        if b.get("type") == "tool_result" and b["tool_use_id"] not in ids
    ]
    assert orphans == []


def test_compact_history_drops_leading_assistant_turn():
    """A slice starting on an assistant message is also invalid as a request."""
    ai = make_assistant()
    ai.conversation = (
        [{"role": "assistant", "content": [{"type": "text", "text": "hi"}]}]
        + [{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}] * 25
    )
    ai._compact_history()
    assert _first_is_plain_user(ai.conversation)


def test_compact_history_leaves_short_history_untouched():
    ai = make_assistant()
    original = _tool_turn(0) + _tool_turn(1)
    ai.conversation = list(original)
    ai._compact_history()
    assert ai.conversation == original


def test_compact_history_handles_all_tool_result_history():
    """Degenerate case: nothing in the window starts clean. Clearing is the
    only valid outcome -- it must not raise or leave an orphan behind."""
    ai = make_assistant()
    ai.conversation = [
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": "ok"}]}
    ] * 45
    ai._compact_history()
    assert ai.conversation == []
