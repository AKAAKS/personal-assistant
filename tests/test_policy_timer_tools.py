"""Permission tiers for the timer and stopwatch tools.

Only the `destructive` tier prompts for confirmation. A timer tool landing there
is the original bug: the user was asked to confirm before every timer.
"""

import pytest

from security.guard import SecurityGuard, Verdict
from security.policy import TOOL_PERMISSIONS

NEW_TOOLS = ("set_timer", "timer_status", "timer_control", "stopwatch_control")


def test_timer_status_is_read_only():
    assert TOOL_PERMISSIONS["timer_status"] == "read_only"


def test_the_mutating_timer_tools_are_write():
    for name in ("set_timer", "timer_control", "stopwatch_control"):
        assert TOOL_PERMISSIONS[name] == "write", name


def test_every_new_tool_has_an_explicit_tier():
    # DEFAULT_PERMISSION happens to be "write" today; an explicit entry means a
    # future change to that default cannot silently re-tier these.
    for name in NEW_TOOLS:
        assert name in TOOL_PERMISSIONS, f"{name} has no explicit tier"


@pytest.mark.parametrize("name,tool_input", [
    ("set_timer", {"minutes": 10}),
    ("timer_status", {}),
    ("timer_control", {"action": "dismiss"}),
    ("timer_control", {"action": "cancel", "id": 3}),
    ("stopwatch_control", {"action": "stop"}),
])
def test_the_guard_allows_the_call_with_no_confirmation_round_trip(name, tool_input):
    # Through the real decision function, not just the table: dismissing a ringing
    # alarm must be instant, on a user turn and on an autonomous one.
    guard = SecurityGuard()
    assert guard.check(name, tool_input).verdict == Verdict.ALLOW
    assert guard.check(name, tool_input, autonomous=True).verdict == Verdict.ALLOW
