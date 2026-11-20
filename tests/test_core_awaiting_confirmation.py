"""Assistant.awaiting_confirmation: the one bit of agent-loop state the server
needs after process() returns, so it knows to keep the mic open for a
destructive-action confirmation."""
from types import SimpleNamespace

from tests.test_core import make_assistant


def assistant_with_guard():
    ai = make_assistant()
    ai.audit = SimpleNamespace(log=lambda *a, **k: None)  # no logs/ writes from tests
    ai._handlers["get_weather"] = lambda ti: "sunny"       # read_only; no network
    return ai


def test_a_confirmation_required_turn_sets_the_flag():
    ai = assistant_with_guard()
    result = ai.execute_guarded("calendar_delete_event", {"id": "x"})  # destructive
    assert result.startswith("CONFIRMATION REQUIRED")
    assert ai.awaiting_confirmation is True


def test_a_new_turn_resets_the_flag():
    ai = assistant_with_guard()
    ai.awaiting_confirmation = True
    ai.process("hello")  # handled locally: no model, no network
    assert ai.awaiting_confirmation is False


def test_an_allowed_tool_leaves_the_flag_false():
    ai = assistant_with_guard()
    assert ai.execute_guarded("get_weather", {}) == "sunny"
    assert ai.awaiting_confirmation is False


def test_an_autonomous_turn_never_sets_the_flag():
    """execute_guarded is shared with the HA trigger rules, which never call
    process(), so a flag set there would leak into the next voice reply."""
    ai = assistant_with_guard()
    result = ai.execute_guarded("calendar_delete_event", {"id": "x"}, autonomous=True)
    assert result.startswith("CONFIRMATION REQUIRED")
    assert ai.awaiting_confirmation is False
