"""Habit-proposal tools: list/accept/dismiss mined-habit suggestions."""

import json

TOOLS = [
    {
        "name": "list_proposals",
        "description": "List pending habit suggestions mined from usage patterns, with their IDs "
        "(use the ID with accept_proposal or dismiss_proposal).",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "accept_proposal",
        "description": "Accept a pending habit suggestion by its ID, applying it (e.g. remembering "
        "a profile fact or adding an automation rule).",
        "input_schema": {
            "type": "object",
            "properties": {
                "id": {
                    "type": "integer",
                    "description": "The ID of the proposal to accept.",
                },
            },
            "required": ["id"],
        },
    },
    {
        "name": "dismiss_proposal",
        "description": "Dismiss a pending habit suggestion by its ID. The suggestion will not be "
        "proposed again.",
        "input_schema": {
            "type": "object",
            "properties": {
                "id": {
                    "type": "integer",
                    "description": "The ID of the proposal to dismiss.",
                },
            },
            "required": ["id"],
        },
    },
]


def handlers(proposal_store=None, rules_store=None, profile_store=None):
    """Proposal tool handlers closing over the stores.

    Args:
        proposal_store: ProposalStore instance, or None if unavailable.
        rules_store: RulesStore instance, used to apply kind='rule' proposals.
        profile_store: ProfileStore instance, used to apply kind='profile' proposals.

    Returns:
        dict: tool name -> callable(tool_input: dict) -> str
    """

    def _is_valid_id(value):
        """True for a plain int id (bool excluded — isinstance(True, int) is True)."""
        return isinstance(value, int) and not isinstance(value, bool)

    def list_proposals(ti):
        """List pending habit proposals."""
        if proposal_store is None:
            return "Error: proposals not available."

        rows = proposal_store.list_pending()
        if not rows:
            return "No pending suggestions."

        lines = []
        for i, row in enumerate(rows, start=1):
            lines.append(f"{i}) {row['proposal_text']} [#{row['id']}]")
        return "\n".join(lines)

    def accept_proposal(ti):
        """Accept a pending proposal, applying its payload."""
        pid = ti.get("id")
        if not _is_valid_id(pid):
            return "Error: id is required and must be an integer."
        if proposal_store is None:
            return "Error: proposals not available."

        row = proposal_store.get(pid)
        if row is None or row["status"] != "pending":
            return f"Error: no pending suggestion with id {pid}."

        payload = json.loads(row["payload"])
        kind = row["kind"]

        if kind == "profile":
            if profile_store is None:
                return "Error: profile not available."
            try:
                profile_store.append(payload["fact"])
            except Exception as exc:
                return f"Error: {exc}"
        elif kind == "rule":
            if rules_store is None:
                return "Error: rules not available."
            try:
                rules_store.add(**payload)
            except Exception as exc:
                return f"Error: {exc}"
        else:
            return f"Error: unknown proposal kind {kind!r}."

        proposal_store.mark_accepted(pid)
        return f"Accepted: {row['proposal_text']}"

    def dismiss_proposal(ti):
        """Dismiss a pending proposal."""
        pid = ti.get("id")
        if not _is_valid_id(pid):
            return "Error: id is required and must be an integer."
        if proposal_store is None:
            return "Error: proposals not available."

        success = proposal_store.mark_dismissed(pid)
        if success:
            return f"Dismissed suggestion #{pid}."
        else:
            return f"Error: no pending suggestion with id {pid}."

    return {
        "list_proposals": list_proposals,
        "accept_proposal": accept_proposal,
        "dismiss_proposal": dismiss_proposal,
    }
