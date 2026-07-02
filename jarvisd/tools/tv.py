"""Android TV tool definitions and handlers."""

import json

TOOLS = [
    {
        "name": "tv_launch_app",
        "description": "Launch an app on the Android TV. Supports common names: netflix, youtube, disney+, prime, hulu, hbo/max, plex, spotify, twitch, chrome, settings.",
        "input_schema": {
            "type": "object",
            "properties": {
                "app_name": {
                    "type": "string",
                    "description": "App name (e.g. 'netflix', 'youtube') or full package name.",
                },
            },
            "required": ["app_name"],
        },
    },
    {
        "name": "tv_close_app",
        "description": "Close/force-stop an app on the Android TV.",
        "input_schema": {
            "type": "object",
            "properties": {
                "app_name": {"type": "string", "description": "App name or package name."},
            },
            "required": ["app_name"],
        },
    },
    {
        "name": "tv_remote",
        "description": "Send remote control key presses to the Android TV. Available keys: up, down, left, right, select, enter, back, home, menu, play, pause, play_pause, stop, next, previous, volume_up, volume_down, mute, power, search.",
        "input_schema": {
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    "description": "Single key to press.",
                },
                "keys": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Multiple keys to press in sequence. Use this for navigation sequences.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "tv_type_text",
        "description": "Type text on the Android TV (for search fields in apps like Netflix, YouTube, etc.). Tip: first launch the app and navigate to its search, then type.",
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Text to type."},
                "clear_first": {
                    "type": "boolean",
                    "description": "Clear existing text before typing. Default false.",
                },
            },
            "required": ["text"],
        },
    },
    {
        "name": "tv_open_url",
        "description": "Open a URL on the Android TV. Can deep-link into apps (e.g. YouTube video URL).",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL to open."},
            },
            "required": ["url"],
        },
    },
    {
        "name": "tv_search_youtube",
        "description": "Search YouTube directly on the Android TV. Opens YouTube with the search results.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to search for on YouTube."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "tv_status",
        "description": "Get the current TV status: screen state (on/off) and which app is in the foreground.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "tv_power",
        "description": "Turn the Android TV on (wake) or off (sleep).",
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["wake", "sleep"],
                },
            },
            "required": ["action"],
        },
    },
]


def handlers(tv):
    """Android TV tool handlers closing over an AndroidTVClient."""

    def tv_launch_app(tool_input):
        return tv.launch_app(tool_input["app_name"])

    def tv_close_app(tool_input):
        return tv.close_app(tool_input["app_name"])

    def tv_remote(tool_input):
        keys = tool_input.get("keys")
        key = tool_input.get("key")
        if keys:
            return tv.send_keys(keys)
        elif key:
            return tv.send_key(key)
        return "No key specified."

    def tv_type_text(tool_input):
        if tool_input.get("clear_first"):
            tv.clear_text()
        return tv.type_text(tool_input["text"])

    def tv_open_url(tool_input):
        return tv.open_url(tool_input["url"])

    def tv_search_youtube(tool_input):
        return tv.search_youtube(tool_input["query"])

    def tv_status(tool_input):
        return json.dumps({"screen": tv.get_screen_state(), "current_app": tv.get_current_app()}, indent=2)

    def tv_power(tool_input):
        if tool_input["action"] == "wake":
            return tv.wake_up()
        return tv.sleep()

    return {
        "tv_launch_app": tv_launch_app,
        "tv_close_app": tv_close_app,
        "tv_remote": tv_remote,
        "tv_type_text": tv_type_text,
        "tv_open_url": tv_open_url,
        "tv_search_youtube": tv_search_youtube,
        "tv_status": tv_status,
        "tv_power": tv_power,
    }
