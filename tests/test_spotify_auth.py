"""Spotify token persistence: cached refresh token must skip the browser flow."""

import json

import pytest

from spotify_client import SpotifyClient


def test_token_cache_round_trip(tmp_path):
    cache = tmp_path / "tok.json"
    c = SpotifyClient("id", "secret")
    c._cache_path = str(cache)
    c.refresh_token = "r123"
    c._save_tokens()

    c2 = SpotifyClient("id", "secret")
    c2._cache_path = str(cache)
    assert c2._load_tokens()
    assert c2.refresh_token == "r123"


def test_ensure_authorized_uses_cache_not_browser(tmp_path, monkeypatch):
    cache = tmp_path / "tok.json"
    cache.write_text(json.dumps({"refresh_token": "r123"}), encoding="utf-8")

    c = SpotifyClient("id", "secret")
    refreshed = []
    monkeypatch.setattr(c, "_refresh_if_needed", lambda: refreshed.append(True))
    monkeypatch.setattr(c, "authorize", lambda: pytest.fail("browser flow must not run"))

    c.ensure_authorized(cache_path=str(cache))
    assert refreshed


def test_ensure_authorized_falls_back_when_no_cache(tmp_path, monkeypatch):
    c = SpotifyClient("id", "secret")
    authorized = []
    monkeypatch.setattr(c, "authorize", lambda: authorized.append(True))

    c.ensure_authorized(cache_path=str(tmp_path / "missing.json"))
    assert authorized


def test_ensure_authorized_falls_back_when_refresh_fails(tmp_path, monkeypatch):
    cache = tmp_path / "tok.json"
    cache.write_text(json.dumps({"refresh_token": "stale"}), encoding="utf-8")

    c = SpotifyClient("id", "secret")
    authorized = []

    def bad_refresh():
        raise RuntimeError("token revoked")

    monkeypatch.setattr(c, "_refresh_if_needed", bad_refresh)
    monkeypatch.setattr(c, "authorize", lambda: authorized.append(True))

    c.ensure_authorized(cache_path=str(cache))
    assert authorized
