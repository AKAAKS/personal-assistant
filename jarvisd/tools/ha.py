"""Home Assistant tool definitions and handlers."""

import json

TOOLS = [
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


def handlers(ha):
    """Home Assistant tool handlers closing over an HomeAssistantClient."""

    def list_devices(tool_input):
        domain = tool_input.get("domain")
        entities = ha.get_entities_by_domain(domain) if domain else ha.get_all_entities()
        summary = []
        for e in entities:
            attrs = e.get("attributes", {})
            name = attrs.get("friendly_name", e["entity_id"])
            summary.append(f"- {name} ({e['entity_id']}): {e['state']}")
        return "\n".join(summary) if summary else "No devices found."

    def get_device_status(tool_input):
        state = ha.get_entity_state(tool_input["entity_id"])
        attrs = state.get("attributes", {})
        name = attrs.get("friendly_name", state["entity_id"])
        return json.dumps({"name": name, "state": state["state"], "attributes": attrs}, indent=2)

    def control_light(tool_input):
        data = {}
        if "brightness" in tool_input:
            data["brightness"] = tool_input["brightness"]
        if "color_name" in tool_input:
            data["color_name"] = tool_input["color_name"]
        ha.call_service("light", tool_input["action"], tool_input["entity_id"], data or None)
        return f"Successfully called {tool_input['action']} on {tool_input['entity_id']}."

    def control_switch(tool_input):
        ha.call_service("switch", tool_input["action"], tool_input["entity_id"])
        return f"Successfully called {tool_input['action']} on {tool_input['entity_id']}."

    def control_media(tool_input):
        data = {}
        if "volume_level" in tool_input:
            data["volume_level"] = tool_input["volume_level"]
        ha.call_service("media_player", tool_input["action"], tool_input["entity_id"], data or None)
        return f"Successfully called {tool_input['action']} on {tool_input['entity_id']}."

    def set_thermostat(tool_input):
        entity_id = tool_input["entity_id"]
        if "hvac_mode" in tool_input:
            ha.call_service("climate", "set_hvac_mode", entity_id, {"hvac_mode": tool_input["hvac_mode"]})
        ha.call_service("climate", "set_temperature", entity_id, {"temperature": tool_input["temperature"]})
        return f"Thermostat {entity_id} set to {tool_input['temperature']} degrees."

    def send_notification(tool_input):
        title = tool_input.get("title", "Assistant")
        ha.send_notification(tool_input["message"], title)
        return f"Notification sent: {title} — {tool_input['message']}"

    return {
        "list_devices": list_devices,
        "get_device_status": get_device_status,
        "control_light": control_light,
        "control_switch": control_switch,
        "control_media": control_media,
        "set_thermostat": set_thermostat,
        "send_notification": send_notification,
    }
