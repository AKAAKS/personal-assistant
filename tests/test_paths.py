"""Tests for the filesystem allowlist used by file tools (security/paths.py)."""
import os

import pytest

from security import paths


@pytest.fixture
def two_roots(tmp_path, monkeypatch):
    root_a = tmp_path / "root_a"
    root_b = tmp_path / "root_b"
    root_a.mkdir()
    root_b.mkdir()
    monkeypatch.setenv("FILE_ROOTS", f"{root_a};{root_b}")
    return root_a, root_b


def test_allowed_roots_reads_env_each_call(two_roots, monkeypatch, tmp_path):
    root_a, root_b = two_roots
    roots = paths.allowed_roots()
    assert root_a.resolve() in roots
    assert root_b.resolve() in roots

    # No module-level caching: changing the env must change the result.
    root_c = tmp_path / "root_c"
    root_c.mkdir()
    monkeypatch.setenv("FILE_ROOTS", str(root_c))
    roots2 = paths.allowed_roots()
    assert roots2 == [root_c.resolve()]


def test_inside_root_is_allowed(two_roots):
    root_a, _ = two_roots
    target = root_a / "notes.txt"
    assert paths.is_allowed_path(str(target)) is True


def test_outside_root_is_blocked(two_roots, tmp_path):
    outside = tmp_path / "elsewhere" / "file.txt"
    assert paths.is_allowed_path(str(outside)) is False


def test_dot_dot_traversal_out_of_root_is_blocked(two_roots):
    root_a, _ = two_roots
    traversal = root_a / ".." / ".." / "Windows" / "system32"
    assert paths.is_allowed_path(str(traversal)) is False


def test_nested_path_inside_root_is_allowed(two_roots):
    root_a, _ = two_roots
    nested = root_a / "sub" / "dir" / "file.txt"
    assert paths.is_allowed_path(str(nested)) is True


@pytest.mark.parametrize(
    "name",
    [
        ".env",
        "id_rsa",
        "id_rsa.pub",
        "server.pem",
        "spotify_tokens.json",
        "my-credentials.txt",
        "some.key",
        "cert.pfx",
        "cert.p12",
        ".netrc",
        ".npmrc",
        ".pypirc",
    ],
)
def test_secret_shaped_names_are_flagged(name):
    assert paths.is_secret_path(name) is True
    assert paths.is_secret_path(f"C:/Users/x/Documents/{name}") is True


@pytest.mark.parametrize("name", ["notes.md", "todo.txt", "report.pdf", "photo.jpg"])
def test_non_secret_names_are_not_flagged(name):
    assert paths.is_secret_path(name) is False
    assert paths.is_secret_path(f"C:/Users/x/Documents/{name}") is False


def test_dot_env_flagged_even_as_path_component():
    # ".env" hidden inside a subdirectory of an otherwise innocuous path.
    assert paths.is_secret_path("proj/.env/config") is True
    assert paths.is_secret_path(os.path.join("proj", ".env", "config")) is True


def test_case_insensitive_secret_match():
    assert paths.is_secret_path("SERVER.PEM") is True
    assert paths.is_secret_path("ID_RSA") is True


# -- Windows-quirk bypasses: trailing dots/spaces and case in components --

@pytest.mark.parametrize("name", [".env ", ".env.", ".env. .", "server.pem ", "server.pem."])
def test_trailing_dots_and_spaces_stripped_before_match(name):
    # Windows CreateFile strips trailing dots/spaces, so ".env " opens ".env".
    assert paths.is_secret_path(name) is True
    assert paths.is_secret_path(f"C:/Users/x/Documents/{name}") is True


def test_dot_env_component_match_is_case_insensitive():
    assert paths.is_secret_path("proj/.ENV/config") is True
    assert paths.is_secret_path("proj/.Env/config") is True


def test_dot_env_component_with_trailing_space_or_dot():
    assert paths.is_secret_path("proj/.env /config") is True
    assert paths.is_secret_path("proj/.env./config") is True
