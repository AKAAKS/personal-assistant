"""Characterization tests: lock v1 security guard behavior before refactor."""
from security.guard import SecurityGuard, Verdict


def test_read_only_tool_allowed():
    g = SecurityGuard()
    assert g.check("list_devices", {}).verdict == Verdict.ALLOW


def test_blocked_keyword():
    g = SecurityGuard()
    r = g.check("send_notification", {"message": "my ssn is 123"})
    assert r.verdict == Verdict.BLOCK


def test_blocked_banking_url():
    g = SecurityGuard()
    r = g.check("open_url_browser", {"url": "https://www.chase.com/login"})
    assert r.verdict == Verdict.BLOCK


def test_unknown_tool_defaults_to_write_allowed():
    g = SecurityGuard()
    assert g.check("some_future_tool", {"x": 1}).verdict == Verdict.ALLOW


def test_destructive_requires_confirm_then_allows():
    g = SecurityGuard()
    # No tool is marked destructive in policy yet; simulate via monkeypatched map.
    from security import policy
    policy.TOOL_PERMISSIONS["fake_destructive"] = "destructive"
    try:
        first = g.check("fake_destructive", {"a": 1})
        second = g.check("fake_destructive", {"a": 1})
        assert first.verdict == Verdict.CONFIRM_REQUIRED
        assert second.verdict == Verdict.ALLOW
    finally:
        del policy.TOOL_PERMISSIONS["fake_destructive"]


def test_autonomous_destructive_never_auto_confirms():
    """The confirm mechanism is retry-based: first check arms, identical second
    check allows. An autonomous caller (trigger rule, reminder) retries with the
    exact same input and no user present -- so on the autonomous path a
    destructive verdict must be terminal CONFIRM_REQUIRED and must never arm
    the pending set."""
    g = SecurityGuard()
    from security import policy
    policy.TOOL_PERMISSIONS["fake_destructive"] = "destructive"
    try:
        first = g.check("fake_destructive", {"a": 1}, autonomous=True)
        second = g.check("fake_destructive", {"a": 1}, autonomous=True)
        assert first.verdict == Verdict.CONFIRM_REQUIRED
        assert second.verdict == Verdict.CONFIRM_REQUIRED
        assert g._pending_confirmations == set()
    finally:
        del policy.TOOL_PERMISSIONS["fake_destructive"]


def test_autonomous_check_does_not_consume_pending_user_confirmation():
    """A rule firing the same action a user has pending must neither consume the
    user's confirmation slot nor be satisfied by it."""
    g = SecurityGuard()
    from security import policy
    policy.TOOL_PERMISSIONS["fake_destructive"] = "destructive"
    try:
        assert g.check("fake_destructive", {"a": 1}).verdict == Verdict.CONFIRM_REQUIRED  # user arms
        assert g.check("fake_destructive", {"a": 1}, autonomous=True).verdict == Verdict.CONFIRM_REQUIRED
        assert g.check("fake_destructive", {"a": 1}).verdict == Verdict.ALLOW  # user's confirm intact
    finally:
        del policy.TOOL_PERMISSIONS["fake_destructive"]
