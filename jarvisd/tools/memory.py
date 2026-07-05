"""Memory tools: remember facts and recall conversations."""

TOOLS = [
    {
        "name": "remember",
        "description": "Store a lasting fact about the user/home (preferences, nicknames, standing instructions) — NOT transient events. The assistant will recall these facts in future conversations.",
        "input_schema": {
            "type": "object",
            "properties": {
                "fact": {
                    "type": "string",
                    "description": "A fact or preference to remember about the user or home.",
                }
            },
            "required": ["fact"],
        },
    },
    {
        "name": "recall",
        "description": "Search past conversations for relevant memories. Use for questions like 'what did we talk about...' or 'do you remember when...'",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query to find relevant memories from past conversations.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of memories to return (default: 5).",
                },
            },
            "required": ["query"],
        },
    },
]


def handlers(profile_store, memory_store):
    """Memory tool handlers closing over profile and memory stores.

    Args:
        profile_store: ProfileStore instance
        memory_store: MemoryStore instance

    Returns:
        dict: tool name -> callable(tool_input: dict) -> str
    """
    def remember(ti):
        """Store a fact in the profile."""
        fact = ti.get("fact", "").strip()
        if not fact:
            return "Remembered: "
        return profile_store.append(fact)

    def recall(ti):
        """Search memory for past conversations."""
        query = ti.get("query", "").strip()
        limit = ti.get("limit", 5)

        if not query:
            return f"No memories matching ''."

        matches = memory_store.search(query, limit=limit)

        if not matches:
            return f"No memories matching '{query}'."

        # Format matches as [ts] role: content
        lines = []
        for match in matches:
            ts = match["ts"]
            role = match["role"]
            content = match["content"]

            # Truncate content to 200 chars
            if len(content) > 200:
                content = content[:200] + "..."

            lines.append(f"[{ts}] {role}: {content}")

        return "\n".join(lines)

    return {
        "remember": remember,
        "recall": recall,
    }
