"""Claude-powered AI assistant with routing, cost tracking, and multi-client tool use."""

import json
import logging
import anthropic
from ha_client import HomeAssistantClient
from spotify_client import SpotifyClient
from tv_client import AndroidTVClient
from system_client import SystemClient
from security.guard import SecurityGuard, Verdict
from security.audit import AuditLogger
from router.classifier import IntentClassifier, Tier
from router.ollama_backend import OllamaBackend
from router.cost_tracker import CostTracker

log = logging.getLogger(__name__)


# --- Tool definitions (sent to Claude so it knows what it can do) ---

HA_TOOLS = [
    {
        "name": "list_devices",
        "description": "List all available Home Assistant devices and their current states. Use this to discover what devices exist and their entity IDs.",
        "input_schema": {
            "type": "object",
            "properties": {
                "domain": {
                    "type": "string",
                    "description": "Optional filter by domain: 'light', 'switch', 'media_player', 'climate', 'sensor', etc. Omit to list all.",
                }
            },
            "required": [],
        },
    },
    {
        "name": "get_device_status",
        "description": "Get the current status of a specific Home Assistant device.",
        "input_schema": {
            "type": "object",
            "properties": {
                "entity_id": {
                    "type": "string",
                    "description": "The Home Assistant entity ID, e.g. 'light.living_room' or 'switch.fan'.",
                }
            },
            "required": ["entity_id"],
        },
    },
    {
        "name": "control_light",
        "description": "Turn a light on or off, and optionally set brightness (0-255) and color.",
        "input_schema": {
            "type": "object",
            "properties": {
                "entity_id": {
                    "type": "string",
                    "description": "The light entity ID, e.g. 'light.bedroom'.",
                },
                "action": {
                    "type": "string",
                    "enum": ["turn_on", "turn_off", "toggle"],
                },
                "brightness": {
                    "type": "integer",
                    "description": "Brightness level 0-255. Only used with turn_on.",
                },
                "color_name": {
                    "type": "string",
                    "description": "Color name like 'red', 'blue', 'warm_white'. Only used with turn_on.",
                },
            },
            "required": ["entity_id", "action"],
        },
    },
    {
        "name": "control_switch",
        "description": "Turn a switch (smart plug, fan, etc.) on or off.",
        "input_schema": {
            "type": "object",
            "properties": {
                "entity_id": {
                    "type": "string",
                    "description": "The switch entity ID, e.g. 'switch.fan'.",
                },
                "action": {
                    "type": "string",
                    "enum": ["turn_on", "turn_off", "toggle"],
                },
            },
            "required": ["entity_id", "action"],
        },
    },
    {
        "name": "control_media",
        "description": "Control a Home Assistant media player (speaker, TV, Chromecast, etc.).",
        "input_schema": {
            "type": "object",
            "properties": {
                "entity_id": {
                    "type": "string",
                    "description": "The media player entity ID, e.g. 'media_player.living_room_speaker'.",
                },
                "action": {
                    "type": "string",
                    "enum": ["media_play", "media_pause", "media_stop", "media_next_track", "media_previous_track", "volume_set", "turn_on", "turn_off"],
                },
                "volume_level": {
                    "type": "number",
                    "description": "Volume level 0.0 to 1.0. Only used with volume_set.",
                },
            },
            "required": ["entity_id", "action"],
        },
    },
    {
        "name": "set_thermostat",
        "description": "Set the target temperature on a climate/thermostat device.",
        "input_schema": {
            "type": "object",
            "properties": {
                "entity_id": {
                    "type": "string",
                    "description": "The climate entity ID, e.g. 'climate.living_room'.",
                },
                "temperature": {
                    "type": "number",
                    "description": "Target temperature in the system's configured unit.",
                },
                "hvac_mode": {
                    "type": "string",
                    "enum": ["heat", "cool", "auto", "off"],
                },
            },
            "required": ["entity_id", "temperature"],
        },
    },
    {
        "name": "send_notification",
        "description": "Send a push notification to the user's phone via the Home Assistant Companion App.",
        "input_schema": {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "The notification message."},
                "title": {"type": "string", "description": "The notification title."},
            },
            "required": ["message"],
        },
    },
]

SPOTIFY_TOOLS = [
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

TV_TOOLS = [
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

SYSTEM_TOOLS = [
    {
        "name": "close_app",
        "description": "Close/kill a running application on the PC. Common names: firefox, chrome, edge, notepad, spotify, discord, steam, vscode, word, excel, teams, slack, obs, vlc, terminal.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "App name, e.g. 'chrome', 'spotify', 'discord'.",
                }
            },
            "required": ["name"],
        },
    },
    {
        "name": "launch_app",
        "description": "Launch an application on the PC. Common names: firefox, chrome, edge, notepad, calculator, settings, spotify, discord, steam, vscode, terminal, task manager, snipping tool.",
        "input_schema": {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "App name or executable path to launch.",
                }
            },
            "required": ["target"],
        },
    },
    {
        "name": "list_processes",
        "description": "List the top 30 running processes on the PC with memory usage.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "open_url_browser",
        "description": "Open a URL in the default web browser on the PC.",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "URL to open.",
                }
            },
            "required": ["url"],
        },
    },
    {
        "name": "system_info",
        "description": "Get current PC system information: CPU usage, RAM usage, disk space.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
]

ALL_TOOLS = HA_TOOLS + SPOTIFY_TOOLS + TV_TOOLS + SYSTEM_TOOLS

SYSTEM_PROMPT = """You are JARVIS, a personal AI assistant. You control the user's smart home, Spotify, Android TV, and PC.

Capabilities:
- **Smart home** (Home Assistant): lights, switches, climate, sensors, notifications
- **Spotify**: search, play on any device, control playback
- **Android TV**: launch/navigate apps, type in search fields, remote control
- **PC control**: launch/close apps, list processes, open URLs, system info

Security policy (strictly enforced):
- NO access to bank accounts, cards, financial services, or sensitive IDs
- Banking/payment/trading URLs are blocked
- Destructive actions require user confirmation

Guidelines:
- Be conversational but concise. Confirm actions after performing them.
- Chain multiple actions when appropriate (e.g. "movie night" = dim lights + open Netflix).
- Spotify: search first for URIs, then play. Target specific devices if asked.
- TV: launch app first, then navigate to search, then type query.
"""


class Assistant:

    MAX_HISTORY = 40

    def __init__(
        self,
        ha_client: HomeAssistantClient,
        anthropic_api_key: str,
        spotify_client: SpotifyClient | None = None,
        tv_client: AndroidTVClient | None = None,
        monthly_budget: float = 20.0,
        ollama_model: str = "qwen2.5:3b-instruct-q4_K_M",
    ):
        self.ha = ha_client
        self.spotify = spotify_client
        self.tv = tv_client
        self.system = SystemClient()
        self.client = anthropic.Anthropic(api_key=anthropic_api_key)
        self.conversation: list[dict] = []
        self.guard = SecurityGuard()
        self.audit = AuditLogger()
        self.classifier = IntentClassifier()
        self.cost_tracker = CostTracker(monthly_budget=monthly_budget)
        self.ollama = OllamaBackend(model=ollama_model)

        self.tools = list(HA_TOOLS) + list(SYSTEM_TOOLS)
        if self.spotify and self.spotify.is_authenticated():
            self.tools.extend(SPOTIFY_TOOLS)
        if self.tv:
            self.tools.extend(TV_TOOLS)

        if self.tools:
            self.tools[-1] = {**self.tools[-1], "cache_control": {"type": "ephemeral"}}

        self._system_prompt = [
            {"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}
        ]

    def _find_spotify_device(self, device_name: str) -> str | None:
        """Find a Spotify device ID by name (case-insensitive partial match)."""
        devices = self.spotify.get_devices()
        name_lower = device_name.lower()
        for d in devices:
            if name_lower in d["name"].lower():
                return d["id"]
        return None

    def _execute_tool(self, tool_name: str, tool_input: dict) -> str:
        """Execute a tool call and return the result as a string."""
        try:
            # --- Home Assistant tools ---
            if tool_name == "list_devices":
                domain = tool_input.get("domain")
                if domain:
                    entities = self.ha.get_entities_by_domain(domain)
                else:
                    entities = self.ha.get_all_entities()
                summary = []
                for e in entities:
                    attrs = e.get("attributes", {})
                    name = attrs.get("friendly_name", e["entity_id"])
                    summary.append(f"- {name} ({e['entity_id']}): {e['state']}")
                return "\n".join(summary) if summary else "No devices found."

            elif tool_name == "get_device_status":
                state = self.ha.get_entity_state(tool_input["entity_id"])
                attrs = state.get("attributes", {})
                name = attrs.get("friendly_name", state["entity_id"])
                return json.dumps({"name": name, "state": state["state"], "attributes": attrs}, indent=2)

            elif tool_name == "control_light":
                entity_id = tool_input["entity_id"]
                action = tool_input["action"]
                data = {}
                if "brightness" in tool_input:
                    data["brightness"] = tool_input["brightness"]
                if "color_name" in tool_input:
                    data["color_name"] = tool_input["color_name"]
                self.ha.call_service("light", action, entity_id, data or None)
                return f"Successfully called {action} on {entity_id}."

            elif tool_name == "control_switch":
                entity_id = tool_input["entity_id"]
                action = tool_input["action"]
                self.ha.call_service("switch", action, entity_id)
                return f"Successfully called {action} on {entity_id}."

            elif tool_name == "control_media":
                entity_id = tool_input["entity_id"]
                action = tool_input["action"]
                data = {}
                if "volume_level" in tool_input:
                    data["volume_level"] = tool_input["volume_level"]
                self.ha.call_service("media_player", action, entity_id, data or None)
                return f"Successfully called {action} on {entity_id}."

            elif tool_name == "set_thermostat":
                entity_id = tool_input["entity_id"]
                data = {"temperature": tool_input["temperature"]}
                if "hvac_mode" in tool_input:
                    self.ha.call_service("climate", "set_hvac_mode", entity_id, {"hvac_mode": tool_input["hvac_mode"]})
                self.ha.call_service("climate", "set_temperature", entity_id, {"temperature": data["temperature"]})
                return f"Thermostat {entity_id} set to {data['temperature']} degrees."

            elif tool_name == "send_notification":
                message = tool_input["message"]
                title = tool_input.get("title", "Assistant")
                self.ha.send_notification(message, title)
                return f"Notification sent: {title} — {message}"

            # --- Spotify tools ---
            elif tool_name == "spotify_search":
                query = tool_input["query"]
                search_type = tool_input.get("search_type", "track")
                results = self.spotify.search(query, search_type)
                if not results:
                    return f"No {search_type} results found for '{query}'."
                return json.dumps(results, indent=2)

            elif tool_name == "spotify_play":
                uri = tool_input.get("uri")
                device_name = tool_input.get("device_name")
                device_id = None
                if device_name:
                    device_id = self._find_spotify_device(device_name)
                    if not device_id:
                        return f"Could not find Spotify device '{device_name}'. Use spotify_devices to list available devices."
                self.spotify.play(uri=uri, device_id=device_id)
                msg = "Resumed playback." if not uri else f"Now playing {uri}."
                if device_name:
                    msg += f" On: {device_name}."
                return msg

            elif tool_name == "spotify_playback_control":
                action = tool_input.get("action")
                volume = tool_input.get("volume_percent")
                if volume is not None:
                    self.spotify.set_volume(volume)
                    return f"Volume set to {volume}%."
                if action == "pause":
                    self.spotify.pause()
                    return "Playback paused."
                elif action == "next":
                    self.spotify.next_track()
                    return "Skipped to next track."
                elif action == "previous":
                    self.spotify.previous_track()
                    return "Went to previous track."
                elif action == "shuffle_on":
                    self.spotify.set_shuffle(True)
                    return "Shuffle enabled."
                elif action == "shuffle_off":
                    self.spotify.set_shuffle(False)
                    return "Shuffle disabled."
                elif action and action.startswith("repeat_"):
                    mode = action.replace("repeat_", "")
                    self.spotify.set_repeat(mode)
                    return f"Repeat set to: {mode}."
                return "No action specified."

            elif tool_name == "spotify_queue":
                self.spotify.add_to_queue(tool_input["uri"])
                return f"Added to queue: {tool_input['uri']}"

            elif tool_name == "spotify_now_playing":
                info = self.spotify.get_current_playback()
                if not info:
                    return "Nothing is currently playing on Spotify."
                return json.dumps(info, indent=2)

            elif tool_name == "spotify_devices":
                devices = self.spotify.get_devices()
                if not devices:
                    return "No Spotify Connect devices found. Make sure Spotify is open on at least one device."
                return json.dumps(devices, indent=2)

            # --- Android TV tools ---
            elif tool_name == "tv_launch_app":
                return self.tv.launch_app(tool_input["app_name"])

            elif tool_name == "tv_close_app":
                return self.tv.close_app(tool_input["app_name"])

            elif tool_name == "tv_remote":
                key = tool_input.get("key")
                keys = tool_input.get("keys")
                if keys:
                    return self.tv.send_keys(keys)
                elif key:
                    return self.tv.send_key(key)
                return "No key specified."

            elif tool_name == "tv_type_text":
                if tool_input.get("clear_first"):
                    self.tv.clear_text()
                return self.tv.type_text(tool_input["text"])

            elif tool_name == "tv_open_url":
                return self.tv.open_url(tool_input["url"])

            elif tool_name == "tv_search_youtube":
                return self.tv.search_youtube(tool_input["query"])

            elif tool_name == "tv_status":
                screen = self.tv.get_screen_state()
                app = self.tv.get_current_app()
                return json.dumps({"screen": screen, "current_app": app}, indent=2)

            elif tool_name == "tv_power":
                if tool_input["action"] == "wake":
                    return self.tv.wake_up()
                else:
                    return self.tv.sleep()

            # --- System / PC tools ---
            elif tool_name == "close_app":
                return self.system.close_app(tool_input["name"])

            elif tool_name == "launch_app":
                return self.system.launch_app(tool_input["target"])

            elif tool_name == "list_processes":
                return self.system.list_processes()

            elif tool_name == "open_url_browser":
                return self.system.open_url(tool_input["url"])

            elif tool_name == "system_info":
                return self.system.system_info()

            else:
                return f"Unknown tool: {tool_name}"

        except Exception as e:
            return f"Error executing {tool_name}: {e}"

    def process(self, user_message: str) -> str:
        """Classify intent, route to cheapest capable tier, return response."""
        tier = self.classifier.classify(user_message)

        if self.cost_tracker.should_force_downgrade() and tier == Tier.SONNET:
            log.warning("Budget near limit — downgrading Sonnet to Haiku")
            tier = Tier.HAIKU

        if tier == Tier.LOCAL:
            resp = self._handle_local(user_message)
            if resp is not None:
                self.conversation.append({"role": "user", "content": user_message})
                self.conversation.append({"role": "assistant", "content": resp})
                return resp
            tier = Tier.HAIKU

        model = "claude-haiku-4-5" if tier == Tier.HAIKU else "claude-sonnet-4-6"
        log.info("Routing to %s", model)
        return self._claude_loop(user_message, model)

    def _handle_local(self, text: str) -> str | None:
        """Handle simple messages without an API call."""
        t = text.lower().strip().rstrip("!?.")

        if t in ("hey", "hi", "hello", "howdy", "yo", "sup"):
            return "Hey! What can I do for you?"
        if t in ("thanks", "thank you", "cheers", "thanks a lot", "thanks so much"):
            return "You're welcome!"
        if t in ("bye", "goodbye", "see ya", "later"):
            return "Goodbye! Let me know if you need anything."
        if t.startswith("good morning"):
            return "Good morning! How can I help today?"
        if t.startswith("good night") or t == "goodnight":
            return "Goodnight! Sleep well."
        if t in ("ok", "ok cool", "cool", "got it", "alright", "sure", "nice", "np", "no problem"):
            return "Let me know if you need anything else!"

        if self.ollama.is_available():
            try:
                return self.ollama.generate(text)
            except Exception as e:
                log.warning("Ollama failed, escalating: %s", e)

        return None

    def _claude_loop(self, user_message: str, model: str) -> str:
        """Agentic tool-use loop with Claude."""
        self.conversation.append({"role": "user", "content": user_message})
        self._compact_history()

        while True:
            response = self.client.messages.create(
                model=model,
                max_tokens=1024,
                system=self._system_prompt,
                tools=self.tools,
                messages=self.conversation,
            )

            self.cost_tracker.record(
                model, response.usage.input_tokens, response.usage.output_tokens,
            )

            assistant_content = response.content
            self.conversation.append({"role": "assistant", "content": assistant_content})

            tool_use_blocks = [b for b in assistant_content if b.type == "tool_use"]

            if not tool_use_blocks:
                text_parts = [b.text for b in assistant_content if b.type == "text"]
                return "\n".join(text_parts)

            tool_results = []
            for tool_block in tool_use_blocks:
                check = self.guard.check(tool_block.name, tool_block.input)

                if check.verdict == Verdict.BLOCK:
                    result = f"SECURITY BLOCK: {check.reason}"
                    self.audit.log(tool_block.name, tool_block.input, "blocked", check.reason)
                elif check.verdict == Verdict.CONFIRM_REQUIRED:
                    result = f"CONFIRMATION REQUIRED: {check.reason}"
                    self.audit.log(tool_block.name, tool_block.input, "confirm_required", check.reason)
                else:
                    result = self._execute_tool(tool_block.name, tool_block.input)
                    self.audit.log(tool_block.name, tool_block.input, "allowed", check.reason, result)

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_block.id,
                    "content": result,
                })

            self.conversation.append({"role": "user", "content": tool_results})

    def _compact_history(self):
        if len(self.conversation) > self.MAX_HISTORY:
            self.conversation = self.conversation[-self.MAX_HISTORY:]

    def reset_conversation(self):
        """Clear conversation history."""
        self.conversation = []
