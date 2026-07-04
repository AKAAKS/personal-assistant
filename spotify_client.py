"""Spotify Web API client for search, playback control, and device targeting."""

import json
import os
import time
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlencode, urlparse, parse_qs
import requests


class SpotifyClient:
    AUTH_URL = "https://accounts.spotify.com/authorize"
    TOKEN_URL = "https://accounts.spotify.com/api/token"
    API_BASE = "https://api.spotify.com/v1"
    SCOPES = (
        "user-read-playback-state "
        "user-modify-playback-state "
        "user-read-currently-playing "
        "playlist-read-private "
        "playlist-read-collaborative"
    )

    def __init__(self, client_id: str, client_secret: str, redirect_uri: str = "http://127.0.0.1:8888/callback"):
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.access_token: str | None = None
        self.refresh_token: str | None = None
        self.token_expiry: float = 0
        self._cache_path: str | None = None

    # --- Authentication ---

    def ensure_authorized(self, cache_path: str = os.path.join("data", "spotify_tokens.json")):
        """Authenticate silently from a cached refresh token, or fall back to the browser flow."""
        self._cache_path = cache_path
        if self._load_tokens():
            try:
                self.token_expiry = 0
                self._refresh_if_needed()
                print("Spotify authenticated from cached token.")
                return
            except Exception:
                print("Cached Spotify token invalid; re-authorizing...")
        self.authorize()

    def _load_tokens(self) -> bool:
        try:
            with open(self._cache_path, encoding="utf-8") as f:
                self.refresh_token = json.load(f).get("refresh_token")
            return bool(self.refresh_token)
        except (OSError, ValueError):
            return False

    def _save_tokens(self):
        if not self._cache_path:
            return
        cache_dir = os.path.dirname(self._cache_path)
        if cache_dir:
            os.makedirs(cache_dir, exist_ok=True)
        with open(self._cache_path, "w", encoding="utf-8") as f:
            json.dump({"refresh_token": self.refresh_token}, f)

    def authorize(self):
        """Open browser for Spotify login and capture the auth code via local redirect."""
        params = {
            "client_id": self.client_id,
            "response_type": "code",
            "redirect_uri": self.redirect_uri,
            "scope": self.SCOPES,
        }
        auth_url = f"{self.AUTH_URL}?{urlencode(params)}"
        print(f"Opening browser for Spotify login...")
        print(f"If it doesn't open, visit: {auth_url}\n")
        webbrowser.open(auth_url)

        # Start a temporary local server to catch the redirect
        auth_code = self._wait_for_auth_code()
        self._exchange_code(auth_code)
        print("Spotify authenticated successfully!\n")

    def _wait_for_auth_code(self) -> str:
        """Run a temporary HTTP server to capture the OAuth callback."""
        code_holder = {}

        class CallbackHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                query = parse_qs(urlparse(self.path).query)
                if "code" in query:
                    code_holder["code"] = query["code"][0]
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                    self.end_headers()
                    self.wfile.write(b"<h2>Spotify connected! You can close this tab.</h2>")
                else:
                    self.send_response(400)
                    self.end_headers()
                    self.wfile.write(b"Authorization failed.")

            def log_message(self, format, *args):
                pass  # Suppress server logs

        parsed = urlparse(self.redirect_uri)
        server = HTTPServer((parsed.hostname, parsed.port), CallbackHandler)
        server.timeout = 120
        while "code" not in code_holder:
            server.handle_request()
        server.server_close()
        return code_holder["code"]

    def _exchange_code(self, code: str):
        """Exchange auth code for access + refresh tokens."""
        resp = requests.post(self.TOKEN_URL, data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.redirect_uri,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        self.access_token = data["access_token"]
        self.refresh_token = data["refresh_token"]
        self.token_expiry = time.time() + data["expires_in"] - 60
        self._save_tokens()

    def _refresh_if_needed(self):
        """Refresh the access token if it's expired."""
        if time.time() < self.token_expiry:
            return
        resp = requests.post(self.TOKEN_URL, data={
            "grant_type": "refresh_token",
            "refresh_token": self.refresh_token,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        self.access_token = data["access_token"]
        self.token_expiry = time.time() + data["expires_in"] - 60
        if "refresh_token" in data:
            self.refresh_token = data["refresh_token"]
            self._save_tokens()

    def _headers(self) -> dict:
        self._refresh_if_needed()
        return {"Authorization": f"Bearer {self.access_token}"}

    def _get(self, path: str, params: dict | None = None) -> dict:
        resp = requests.get(f"{self.API_BASE}{path}", headers=self._headers(), params=params, timeout=10)
        resp.raise_for_status()
        return resp.json()

    def _put(self, path: str, json_data: dict | None = None, params: dict | None = None):
        resp = requests.put(
            f"{self.API_BASE}{path}", headers=self._headers(), json=json_data, params=params, timeout=10
        )
        resp.raise_for_status()

    def _post(self, path: str, json_data: dict | None = None, params: dict | None = None):
        resp = requests.post(
            f"{self.API_BASE}{path}", headers=self._headers(), json=json_data, params=params, timeout=10
        )
        resp.raise_for_status()

    # --- Search ---

    def search(self, query: str, search_type: str = "track", limit: int = 5) -> list[dict]:
        """Search Spotify for tracks, albums, artists, or playlists.

        Returns a simplified list of results.
        """
        data = self._get("/search", {"q": query, "type": search_type, "limit": limit})

        results = []
        items_key = f"{search_type}s"
        for item in data.get(items_key, {}).get("items", []):
            entry = {"uri": item["uri"], "name": item["name"], "type": search_type}
            if search_type == "track":
                entry["artist"] = ", ".join(a["name"] for a in item.get("artists", []))
                entry["album"] = item.get("album", {}).get("name", "")
                entry["duration_ms"] = item.get("duration_ms")
            elif search_type == "album":
                entry["artist"] = ", ".join(a["name"] for a in item.get("artists", []))
                entry["total_tracks"] = item.get("total_tracks")
            elif search_type == "artist":
                entry["genres"] = item.get("genres", [])
            elif search_type == "playlist":
                entry["owner"] = item.get("owner", {}).get("display_name", "")
                entry["total_tracks"] = item.get("tracks", {}).get("total", 0)
            results.append(entry)

        return results

    # --- Playback control ---

    def get_devices(self) -> list[dict]:
        """List available Spotify Connect devices."""
        data = self._get("/me/player/devices")
        return [
            {"id": d["id"], "name": d["name"], "type": d["type"], "is_active": d["is_active"]}
            for d in data.get("devices", [])
        ]

    def get_current_playback(self) -> dict | None:
        """Get info about what's currently playing."""
        try:
            data = self._get("/me/player")
            if not data:
                return None
            item = data.get("item", {})
            return {
                "is_playing": data.get("is_playing"),
                "track": item.get("name"),
                "artist": ", ".join(a["name"] for a in item.get("artists", [])),
                "album": item.get("album", {}).get("name"),
                "device": data.get("device", {}).get("name"),
                "progress_ms": data.get("progress_ms"),
                "duration_ms": item.get("duration_ms"),
                "shuffle": data.get("shuffle_state"),
                "repeat": data.get("repeat_state"),
            }
        except requests.HTTPError:
            return None

    def play(self, uri: str | None = None, device_id: str | None = None):
        """Start or resume playback. Optionally play a specific URI on a specific device.

        URI can be a track, album, playlist, or artist URI.
        """
        params = {"device_id": device_id} if device_id else {}
        body = None
        if uri:
            if ":track:" in uri:
                body = {"uris": [uri]}
            else:
                body = {"context_uri": uri}
        self._put("/me/player/play", json_data=body, params=params)

    def pause(self, device_id: str | None = None):
        params = {"device_id": device_id} if device_id else {}
        self._put("/me/player/pause", params=params)

    def next_track(self, device_id: str | None = None):
        params = {"device_id": device_id} if device_id else {}
        self._post("/me/player/next", params=params)

    def previous_track(self, device_id: str | None = None):
        params = {"device_id": device_id} if device_id else {}
        self._post("/me/player/previous", params=params)

    def set_volume(self, volume_percent: int, device_id: str | None = None):
        """Set volume (0-100)."""
        params = {"volume_percent": max(0, min(100, volume_percent))}
        if device_id:
            params["device_id"] = device_id
        self._put("/me/player/volume", params=params)

    def set_shuffle(self, state: bool, device_id: str | None = None):
        params = {"state": str(state).lower()}
        if device_id:
            params["device_id"] = device_id
        self._put("/me/player/shuffle", params=params)

    def set_repeat(self, state: str = "off", device_id: str | None = None):
        """Set repeat mode: 'off', 'track', or 'context'."""
        params = {"state": state}
        if device_id:
            params["device_id"] = device_id
        self._put("/me/player/repeat", params=params)

    def add_to_queue(self, uri: str, device_id: str | None = None):
        """Add a track to the playback queue."""
        params = {"uri": uri}
        if device_id:
            params["device_id"] = device_id
        self._post("/me/player/queue", params=params)

    def transfer_playback(self, device_id: str, start_playing: bool = True):
        """Transfer playback to a different device."""
        self._put("/me/player", json_data={"device_ids": [device_id], "play": start_playing})

    def is_authenticated(self) -> bool:
        return self.access_token is not None
