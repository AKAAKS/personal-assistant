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
