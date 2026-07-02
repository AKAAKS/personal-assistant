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
