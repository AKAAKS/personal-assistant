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
