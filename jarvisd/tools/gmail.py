"""Gmail tool definitions and handlers — read + draft only.

JARVIS can search and read mail and compose drafts, but it deliberately has no
send capability (the GoogleClient is built with the gmail.compose scope, which
creates drafts but cannot send). The user reviews and sends from Gmail.
"""

TOOLS = [
    {
        "name": "gmail_search",
        "description": (
            "Search the user's Gmail. Use Gmail search syntax (e.g. 'is:unread', "
            "'from:alice', 'subject:invoice newer_than:7d'). Returns up to 10 matching "
            "messages with their id, sender, subject, date, and a snippet. Use gmail_read "
            "with an id to read a full message."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Gmail search query, e.g. 'is:unread from:alice'.",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Max messages to return (default 10).",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "gmail_read",
        "description": "Read the full content of one Gmail message by its id (from gmail_search).",
        "input_schema": {
            "type": "object",
            "properties": {
                "message_id": {
                    "type": "string",
                    "description": "The message id returned by gmail_search.",
                },
            },
            "required": ["message_id"],
        },
    },
    {
        "name": "gmail_draft",
        "description": (
            "Compose a Gmail draft (does NOT send — the user reviews and sends it in Gmail). "
            "Use this for replies and new emails."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "Recipient email address."},
                "subject": {"type": "string", "description": "Email subject line."},
                "body": {"type": "string", "description": "Email body text."},
            },
            "required": ["to", "subject", "body"],
        },
    },
]

MAX_RESULTS = 10


def handlers(google_client):
    """Gmail tool handlers closing over a GoogleClient."""

    def gmail_search(ti):
        query = ti.get("query")
        if not isinstance(query, str) or not query.strip():
            return "Error: query is required and must be a non-empty string."
        max_results = ti.get("max_results")
        if not isinstance(max_results, int) or max_results <= 0:
            max_results = MAX_RESULTS
        try:
            messages = google_client.search_messages(query.strip(), max_results=max_results)
        except Exception as e:
            return f"Error searching Gmail: {e}"
        if not messages:
            return f"No messages found for '{query}'."
        lines = []
        for m in messages:
            lines.append(
                f"[{m.get('id')}] {m.get('date', '')} — from {m.get('from', '?')}: "
                f"{m.get('subject', '(no subject)')}\n    {m.get('snippet', '')}"
            )
        return "\n".join(lines)

    def gmail_read(ti):
        message_id = ti.get("message_id")
        if not isinstance(message_id, str) or not message_id.strip():
            return "Error: message_id is required and must be a non-empty string."
        try:
            msg = google_client.get_message(message_id.strip())
        except Exception as e:
            return f"Error reading message: {e}"
        if not msg:
            return f"Message '{message_id}' not found."
        return (
            f"From: {msg.get('from', '?')}\n"
            f"To: {msg.get('to', '?')}\n"
            f"Date: {msg.get('date', '?')}\n"
            f"Subject: {msg.get('subject', '(no subject)')}\n\n"
            f"{msg.get('body', '')}"
        )

    def gmail_draft(ti):
        to = ti.get("to")
        subject = ti.get("subject")
        body = ti.get("body")
        for name, val in (("to", to), ("subject", subject), ("body", body)):
            if not isinstance(val, str) or not val.strip():
                return f"Error: {name} is required and must be a non-empty string."
        try:
            google_client.create_draft(to.strip(), subject.strip(), body)
        except Exception as e:
            return f"Error creating draft: {e}"
        return (
            f"Draft created to {to.strip()} with subject \"{subject.strip()}\". "
            "It was NOT sent — review and send it from Gmail when you're ready."
        )

    return {
        "gmail_search": gmail_search,
        "gmail_read": gmail_read,
        "gmail_draft": gmail_draft,
    }
