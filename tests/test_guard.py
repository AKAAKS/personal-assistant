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
        g.begin_turn()
        first = g.check("fake_destructive", {"a": 1})
        g.begin_turn()  # user's next turn confirms
        second = g.check("fake_destructive", {"a": 1})
        assert first.verdict == Verdict.CONFIRM_REQUIRED
        assert second.verdict == Verdict.ALLOW
    finally:
        del policy.TOOL_PERMISSIONS["fake_destructive"]


def test_destructive_same_turn_retry_does_not_confirm():
    """An identical retry WITHIN one turn (the model looping, no turn boundary)
    must re-prompt, never auto-confirm — otherwise the model self-confirms its
    own destructive action with no human involved."""
    g = SecurityGuard()
    from security import policy
    policy.TOOL_PERMISSIONS["fake_destructive"] = "destructive"
    try:
        g.begin_turn()
        first = g.check("fake_destructive", {"a": 1})
        second = g.check("fake_destructive", {"a": 1})  # same turn, no begin_turn
        assert first.verdict == Verdict.CONFIRM_REQUIRED
        assert second.verdict == Verdict.CONFIRM_REQUIRED
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
        assert not g._pending_confirmations  # autonomous path never arms
    finally:
        del policy.TOOL_PERMISSIONS["fake_destructive"]


def test_autonomous_check_does_not_consume_pending_user_confirmation():
    """A rule firing the same action a user has pending must neither consume the
    user's confirmation slot nor be satisfied by it."""
    g = SecurityGuard()
    from security import policy
    policy.TOOL_PERMISSIONS["fake_destructive"] = "destructive"
    try:
        g.begin_turn()
        assert g.check("fake_destructive", {"a": 1}).verdict == Verdict.CONFIRM_REQUIRED  # user arms
        assert g.check("fake_destructive", {"a": 1}, autonomous=True).verdict == Verdict.CONFIRM_REQUIRED
        g.begin_turn()  # user's next turn confirms
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
    g.begin_turn()
    first = g.check("run_script", {"script": "echo hi"})
    g.begin_turn()  # user's next turn confirms
    second = g.check("run_script", {"script": "echo hi"})
    assert first.verdict == Verdict.CONFIRM_REQUIRED
    assert second.verdict == Verdict.ALLOW


def test_run_script_autonomous_stays_terminal_confirm_required(file_root):
    g = SecurityGuard()
    first = g.check("run_script", {"script": "echo hi"}, autonomous=True)
    second = g.check("run_script", {"script": "echo hi"}, autonomous=True)
    assert first.verdict == Verdict.CONFIRM_REQUIRED
    assert second.verdict == Verdict.CONFIRM_REQUIRED
    assert not g._pending_confirmations  # autonomous path never arms


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


# -- Windows-quirk secret-file bypasses (canonicalize before checking) --

@pytest.fixture
def planted_env(file_root):
    """A real .env file (fabricated contents) inside the allowed root."""
    env_file = file_root / ".env"
    env_file.write_text("FAKE_KEY=fabricated-value\n")
    return env_file


def test_read_document_env_with_trailing_space_is_blocked(planted_env):
    # Windows CreateFile strips trailing spaces: ".env " opens ".env".
    g = SecurityGuard()
    r = g.check("read_document", {"path": str(planted_env) + " "})
    assert r.verdict == Verdict.BLOCK


def test_read_document_env_with_trailing_dot_is_blocked(planted_env):
    # Windows CreateFile strips trailing dots: ".env." opens ".env".
    g = SecurityGuard()
    r = g.check("read_document", {"path": str(planted_env) + "."})
    assert r.verdict == Verdict.BLOCK


def test_read_document_env_via_short_name_is_blocked(planted_env):
    # 8.3 short names ("ENV~1") alias the real file; resolve() expands them.
    import ctypes
    from pathlib import Path

    from security.paths import is_secret_path

    buf = ctypes.create_unicode_buffer(260)
    rc = ctypes.windll.kernel32.GetShortPathNameW(str(planted_env), buf, 260)
    if not rc:
        pytest.skip("could not obtain a short path name")
    short = buf.value
    if short.lower() == str(planted_env).lower():
        pytest.skip("8.3 short names disabled on this filesystem")
    # Sanity: canonicalization of the short spelling must land on the secret.
    assert is_secret_path(str(Path(short).resolve())) is True

    g = SecurityGuard()
    r = g.check("read_document", {"path": short})
    assert r.verdict == Verdict.BLOCK


def test_read_document_under_upper_case_env_dir_is_blocked(file_root):
    # ".ENV" must not defeat a case-sensitive ".env" component check.
    envdir = file_root / ".ENV"
    envdir.mkdir()
    inner = envdir / "config"
    inner.write_text("x")
    g = SecurityGuard()
    r = g.check("read_document", {"path": str(inner)})
    assert r.verdict == Verdict.BLOCK
