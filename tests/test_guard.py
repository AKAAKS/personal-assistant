"""Characterization tests: lock v1 security guard behavior before refactor."""
import pytest

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


# -- Task 6: path allowlist + secret-file hard block --

@pytest.fixture
def file_root(tmp_path, monkeypatch):
    root = tmp_path / "allowed_root"
    root.mkdir()
    monkeypatch.setenv("FILE_ROOTS", str(root))
    return root


def test_read_document_outside_roots_is_blocked(file_root, tmp_path):
    g = SecurityGuard()
    outside = tmp_path / "elsewhere" / "notes.txt"
    r = g.check("read_document", {"path": str(outside)})
    assert r.verdict == Verdict.BLOCK
    assert "outside the allowed folders" in r.reason


def test_read_document_inside_roots_is_allowed(file_root):
    g = SecurityGuard()
    inside = file_root / "notes.txt"
    r = g.check("read_document", {"path": str(inside)})
    assert r.verdict == Verdict.ALLOW


def test_read_document_dot_env_inside_roots_is_blocked(file_root):
    g = SecurityGuard()
    secret = file_root / "proj" / ".env"
    r = g.check("read_document", {"path": str(secret)})
    assert r.verdict == Verdict.BLOCK
    assert "secret-shaped files" in r.reason


def test_any_tool_with_dot_env_path_string_is_blocked():
    g = SecurityGuard()
    r = g.check("send_notification", {"message": "C:/proj/.env"})
    assert r.verdict == Verdict.BLOCK
    assert "secret-shaped files" in r.reason


def test_secret_check_does_not_trip_on_plain_urls():
    g = SecurityGuard()
    r = g.check("send_notification", {"message": "https://example.com/page"})
    assert r.verdict == Verdict.ALLOW


def test_search_files_missing_directory_key_is_not_path_checked():
    g = SecurityGuard()
    r = g.check("search_files", {})
    assert r.verdict == Verdict.ALLOW


def test_run_script_is_destructive_requires_confirm_then_allows(file_root):
    g = SecurityGuard()
    first = g.check("run_script", {"script": "echo hi"})
    second = g.check("run_script", {"script": "echo hi"})
    assert first.verdict == Verdict.CONFIRM_REQUIRED
    assert second.verdict == Verdict.ALLOW


def test_run_script_autonomous_stays_terminal_confirm_required(file_root):
    g = SecurityGuard()
    first = g.check("run_script", {"script": "echo hi"}, autonomous=True)
    second = g.check("run_script", {"script": "echo hi"}, autonomous=True)
    assert first.verdict == Verdict.CONFIRM_REQUIRED
    assert second.verdict == Verdict.CONFIRM_REQUIRED
    assert g._pending_confirmations == set()


def test_malformed_path_is_blocked_not_raised(file_root):
    g = SecurityGuard()
    malformed = "C:" + chr(0) + "bad"  # embedded NUL -- resolve() raises ValueError
    r = g.check("read_document", {"path": malformed})
    assert r.verdict == Verdict.BLOCK
    assert "could not be validated" in r.reason


def test_non_string_path_value_does_not_raise(file_root):
    g = SecurityGuard()
    r = g.check("read_document", {"path": 12345})
    assert r.verdict in (Verdict.ALLOW, Verdict.BLOCK)
