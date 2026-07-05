"""Trigger rule tools: add, list, and delete automation rules."""

TOOLS = [
    {
        "name": "add_trigger_rule",
        "description": """Add an automation rule triggered by Home Assistant events.

Two kinds of rules:
1. **Direct rules** execute a single tool call directly. Action is a JSON string with {"tool": "<tool_name>", "input": {...}}, e.g.:
   {"tool": "control_light", "input": {"entity_id": "light.hall", "action": "turn_on"}}
   These run immediately when the event matches, bypassing the AI.

2. **Smart rules** pass a natural-language instruction to the AI. The AI processes the instruction and decides what to do.
   E.g., action: "If the front door opens when nobody is home, notify the user and check the camera."
   Smart rules are rate-limited (cooldown applies) to avoid spam.

Both rule types can filter on entity_id and new state (to_state).
""",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Human-readable name for the rule (e.g., 'Hall light on motion').",
                },
                "event_type": {
                    "type": "string",
                    "description": "Home Assistant event type to match (e.g., 'state_changed').",
                },
                "kind": {
                    "type": "string",
                    "enum": ["direct", "smart"],
                    "description": "Rule kind: 'direct' (execute tool) or 'smart' (pass to AI).",
                },
                "action": {
                    "type": "string",
                    "description": "For direct: JSON string with tool call. For smart: natural-language instruction.",
                },
                "entity_id": {
                    "type": "string",
                    "description": "Optional: filter by entity_id (e.g., 'motion.hall'). If omitted, rule matches any entity.",
                },
                "to_state": {
                    "type": "string",
                    "description": "Optional: filter by new state (e.g., 'on'). If omitted, rule matches any state.",
                },
                "cooldown_sec": {
                    "type": "integer",
                    "description": "Minimum seconds between fires (default 300). Prevents repeated triggers.",
                },
            },
            "required": ["name", "event_type", "kind", "action"],
        },
    },
    {
        "name": "list_trigger_rules",
        "description": "List all trigger rules.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "delete_trigger_rule",
        "description": "Delete a trigger rule by its ID.",
        "input_schema": {
            "type": "object",
            "properties": {
                "rule_id": {
                    "type": "integer",
                    "description": "The ID of the rule to delete.",
                },
            },
            "required": ["rule_id"],
        },
    },
]


def handlers(rules_store):
    """Trigger rule tool handlers closing over rules_store.

    Args:
        rules_store: RulesStore instance

    Returns:
        dict: tool name -> callable(tool_input: dict) -> str
    """

    def add_trigger_rule(ti):
        """Add a trigger rule."""
        name = ti.get("name", "").strip()
        event_type = ti.get("event_type", "").strip()
        kind = ti.get("kind", "").strip()
        action = ti.get("action", "").strip()
        entity_id = ti.get("entity_id")
        to_state = ti.get("to_state")
        cooldown_sec = ti.get("cooldown_sec", 300)

        if not name:
            return "Error: name is required."
        if not event_type:
            return "Error: event_type is required."
        if not kind:
            return "Error: kind is required."
        if not action:
            return "Error: action is required."

        try:
            rule_id = rules_store.add(
                name,
                event_type,
                kind,
                action,
                entity_id=entity_id,
                to_state=to_state,
                cooldown_sec=cooldown_sec,
            )
            return f"Rule #{rule_id} '{name}' added."
        except ValueError as exc:
            return f"Error: {exc}"

    def list_trigger_rules(ti):
        """List all trigger rules."""
        rules = rules_store.list_all()

        if not rules:
            return "No trigger rules configured."

        lines = []
        for i, rule in enumerate(rules, start=1):
            name = rule["name"]
            event_type = rule["event_type"]
            kind = rule["kind"]
            enabled_str = "enabled" if rule["enabled"] else "disabled"
            filters = []
            if rule["entity_id"]:
                filters.append(f"entity:{rule['entity_id']}")
            if rule["to_state"]:
                filters.append(f"to_state:{rule['to_state']}")
            filters_str = f" [{', '.join(filters)}]" if filters else ""
            lines.append(f"{i}. {name} ({kind}, {event_type}){filters_str} — {enabled_str}")

        return "\n".join(lines)

    def delete_trigger_rule(ti):
        """Delete a trigger rule."""
        rule_id = ti.get("rule_id")

        if rule_id is None:
            return "Error: rule_id is required."

        success = rules_store.delete(rule_id)
        if success:
            return f"Rule #{rule_id} deleted."
        else:
            return f"Error: rule #{rule_id} not found."

    return {
        "add_trigger_rule": add_trigger_rule,
        "list_trigger_rules": list_trigger_rules,
        "delete_trigger_rule": delete_trigger_rule,
    }
