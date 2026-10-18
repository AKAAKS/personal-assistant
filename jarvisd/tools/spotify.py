"""Spotify tool definitions and handlers."""

import json

TOOLS = [
    {
        "name": "spotify_search",
        "description": "Search Spotify for songs, albums, artists, or playlists. Returns up to 5 results with names and URIs.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query, e.g. 'Bohemian Rhapsody', 'lo-fi playlist', 'Taylor Swift'.",
                },
                "search_type": {
                    "type": "string",
                    "enum": ["track", "album", "artist", "playlist"],
                    "description": "What to search for. Default: track.",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "spotify_play",
        "description": "Play a specific song, album, playlist, or artist on Spotify. Can target a specific device. If no URI given, resumes current playback.",
        "input_schema": {
            "type": "object",
            "properties": {
                "uri": {
                    "type": "string",
                    "description": "Spotify URI to play (e.g. 'spotify:track:...'). Use spotify_search to find URIs. Omit to resume.",
                },
                "device_name": {
                    "type": "string",
                    "description": "Name of the Spotify Connect device to play on (e.g. 'Living Room Speaker'). Omit for current device.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "spotify_playback_control",
        "description": "Control Spotify playback: pause, skip, previous, volume, shuffle, repeat.",
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["pause", "next", "previous", "shuffle_on", "shuffle_off", "repeat_off", "repeat_track", "repeat_context"],
                },
                "volume_percent": {
                    "type": "integer",
                    "description": "Set volume 0-100. Use this instead of action when adjusting volume.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "spotify_queue",
        "description": "Add a song to the Spotify playback queue.",
        "input_schema": {
            "type": "object",
            "properties": {
                "uri": {
                    "type": "string",
                    "description": "Spotify track URI to add to queue.",
                },
            },
            "required": ["uri"],
        },
    },
    {
        "name": "spotify_now_playing",
        "description": "Get info about the currently playing track on Spotify.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "spotify_devices",
        "description": "List all available Spotify Connect devices (speakers, phones, computers, TVs).",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
]


def _find_device(spotify, device_name):
    for d in spotify.get_devices():
        if device_name.lower() in d["name"].lower():
            return d["id"]
    return None


_NO_DEVICE_MSG = (
    "No active Spotify device found. Open Spotify on your phone or computer and "
    "start playing anything once so it registers as a device, then ask again. "
    "(Spotify playback control also requires a Premium account.)"
)


def _pick_device(spotify):
    """Best available playback target as (device_id, device_name), or (None, None).

    Prefers the already-active device, else the first one Spotify lists.
    """
    try:
        devices = spotify.get_devices()
    except Exception:
        devices = []
    if not devices:
        return None, None
    target = next((d for d in devices if d.get("is_active")), devices[0])
    return target["id"], target["name"]


def handlers(spotify):
    """Spotify tool handlers closing over a SpotifyClient."""

    def spotify_search(tool_input):
        query = tool_input["query"]
        search_type = tool_input.get("search_type", "track")
        results = spotify.search(query, search_type)
        if not results:
            return f"No {search_type} results found for '{query}'."
        return json.dumps(results, indent=2)

    def spotify_play(tool_input):
        uri = tool_input.get("uri")
        device_name = tool_input.get("device_name")
        if device_name:
            device_id = _find_device(spotify, device_name)
            if not device_id:
                return (f"Could not find Spotify device '{device_name}'. "
                        "Use spotify_devices to list available devices.")
            resolved_name = device_name
        else:
            # No device specified: resolve the best available target up front.
            # A bare /me/player/play with no *active* device returns 204 but
            # silently no-ops, so targeting a concrete device_id is what actually
            # starts audio (and lets us report honestly instead of a false
            # "Now playing"). If there's no device at all, say so.
            device_id, resolved_name = _pick_device(spotify)
            if device_id is None:
                return _NO_DEVICE_MSG
        try:
            spotify.play(uri=uri, device_id=device_id)
        except Exception as e:
            return f"Error playing on Spotify: {e}"
        base = "Resumed playback." if not uri else f"Now playing {uri}."
        return base + f" On: {resolved_name}."

    def spotify_playback_control(tool_input):
        action = tool_input.get("action")
        volume = tool_input.get("volume_percent")
        if volume is not None:
            spotify.set_volume(volume)
            return f"Volume set to {volume}%."
        if action == "pause":
            spotify.pause()
            return "Playback paused."
        elif action == "next":
            spotify.next_track()
            return "Skipped to next track."
        elif action == "previous":
            spotify.previous_track()
            return "Went to previous track."
        elif action == "shuffle_on":
            spotify.set_shuffle(True)
            return "Shuffle enabled."
        elif action == "shuffle_off":
            spotify.set_shuffle(False)
            return "Shuffle disabled."
        elif action and action.startswith("repeat_"):
            mode = action.replace("repeat_", "")
            spotify.set_repeat(mode)
            return f"Repeat set to: {mode}."
        return "No action specified."

    def spotify_queue(tool_input):
        uri = tool_input["uri"]
        try:
            spotify.add_to_queue(uri)
        except Exception:
            # Same "no active device" recovery as spotify_play.
            rid, rname = _pick_device(spotify)
            if rid is None:
                return _NO_DEVICE_MSG
            try:
                spotify.add_to_queue(uri, device_id=rid)
            except Exception as e2:
                return f"Error adding to Spotify queue: {e2}"
            return f"Added to queue: {uri} (on {rname})."
        return f"Added to queue: {uri}"

    def spotify_now_playing(tool_input):
        info = spotify.get_current_playback()
        if not info:
            return "Nothing is currently playing on Spotify."
        return json.dumps(info, indent=2)

    def spotify_devices(tool_input):
        devices = spotify.get_devices()
        if not devices:
            return "No Spotify Connect devices found. Make sure Spotify is open on at least one device."
        return json.dumps(devices, indent=2)

    return {
        "spotify_search": spotify_search,
        "spotify_play": spotify_play,
        "spotify_playback_control": spotify_playback_control,
        "spotify_queue": spotify_queue,
        "spotify_now_playing": spotify_now_playing,
        "spotify_devices": spotify_devices,
    }
