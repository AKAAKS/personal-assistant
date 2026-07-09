"""Trigger rules: Home Assistant event-driven automation stored in SQLite."""

import sqlite3
import json
from datetime import datetime


class RulesStore:
    """Stores trigger rules and serves add/list/delete/matches/cooldown operations."""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def add(
        self,
        name: str,
        event_type: str,
        kind: str,
        action: str,
        entity_id: str = None,
        to_state: str = None,
        cooldown_sec: int = 300,
    ) -> int:
        """Add a trigger rule.

        Args:
            name: Human-readable rule name.
            event_type: HA event type to match (e.g., 'state_changed').
            kind: Rule type: 'direct' or 'smart'.
            action: For direct: JSON string with {"tool": str, "input": dict}.
                   For smart: free-text instruction.
            entity_id: Optional entity_id filter.
            to_state: Optional new state filter.
            cooldown_sec: Minimum seconds between fires (default 300).
                   Smart rules are clamped to at least 60.

        Returns:
            int: new rule id

        Raises:
            ValueError: if kind is invalid, or if direct action is invalid JSON.
        """
        if kind not in ("direct", "smart"):
            raise ValueError(f"kind must be 'direct' or 'smart', got {kind!r}")

        # Validate direct rule action at add time
        if kind == "direct":
            self._validate_direct_action(action)

        # Smart rules cost an AI call per fire; floor the cooldown so a chatty
        # entity (or an over-eager model picking cooldown_sec=1) can't burn the
        # monthly budget continuously. Direct rules are $0 and stay unclamped.
        if kind == "smart":
            cooldown_sec = max(cooldown_sec, 60)

        created_ts = datetime.now().isoformat(timespec="seconds")
        cur = self._conn.execute(
            """INSERT INTO trigger_rules
            (name, event_type, entity_id, to_state, kind, action, cooldown_sec, enabled, created_ts)
            VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)""",
            (name, event_type, entity_id, to_state, kind, action, cooldown_sec, created_ts),
        )
        self._conn.commit()
        return cur.lastrowid

    def list_enabled(self) -> list[sqlite3.Row]:
        """List all enabled rules."""
        return self._conn.execute(
            "SELECT * FROM trigger_rules WHERE enabled = 1 ORDER BY created_ts DESC"
        ).fetchall()

    def list_all(self) -> list[sqlite3.Row]:
        """List all rules regardless of enabled status."""
        return self._conn.execute(
            "SELECT * FROM trigger_rules ORDER BY created_ts DESC"
        ).fetchall()

    def delete(self, rule_id: int) -> bool:
        """Delete a rule by id.

        Args:
            rule_id: The rule id to delete.

        Returns:
            bool: True if deleted, False if not found.
        """
        cur = self._conn.execute(
            "DELETE FROM trigger_rules WHERE id = ?",
            (rule_id,),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def matches(self, event: dict) -> list[sqlite3.Row]:
        """Find all enabled rules matching an event.

        Event dict shape: {"event_type": str, "entity_id": str | None, "to_state": str | None}

        Rules match if:
        - event_type equals rule's event_type
        - rule has no entity_id filter, OR event's entity_id equals rule's entity_id
        - rule has no to_state filter, OR event's to_state equals rule's to_state
        - rule is enabled

        Returns:
            list[sqlite3.Row]: matching rules
        """
        event_type = event["event_type"]
        entity_id = event.get("entity_id")
        to_state = event.get("to_state")

        # Start with event_type match and enabled filter
        rows = self._conn.execute(
            "SELECT * FROM trigger_rules WHERE event_type = ? AND enabled = 1",
            (event_type,),
        ).fetchall()

        matches = []
        for row in rows:
            # Filter by entity_id if rule has one
            if row["entity_id"] is not None and row["entity_id"] != entity_id:
                continue
            # Filter by to_state if rule has one
            if row["to_state"] is not None and row["to_state"] != to_state:
                continue
            matches.append(row)

        return matches

    def check_and_stamp_cooldown(self, rule_id: int, now: datetime) -> bool:
        """Check if a rule can fire now, and stamp last_fired_ts if allowed.

        A rule can fire if last_fired_ts is NULL (never fired) or
        (now - last_fired_ts >= cooldown_sec).

        Args:
            rule_id: The rule to check.
            now: Current datetime.

        Returns:
            bool: True if allowed to fire (and stamped), False otherwise (not stamped).
        """
        row = self._conn.execute(
            "SELECT last_fired_ts, cooldown_sec FROM trigger_rules WHERE id = ?",
            (rule_id,),
        ).fetchone()

        if row is None:
            return False

        last_fired = row["last_fired_ts"]
        cooldown_sec = row["cooldown_sec"]

        # First fire is always allowed
        if last_fired is None:
            now_str = now.isoformat(timespec="seconds")
            self._conn.execute(
                "UPDATE trigger_rules SET last_fired_ts = ? WHERE id = ?",
                (now_str, rule_id),
            )
            self._conn.commit()
            return True

        # Check elapsed time
        last_fired_dt = datetime.fromisoformat(last_fired)
        elapsed = (now - last_fired_dt).total_seconds()
        if elapsed >= cooldown_sec:
            now_str = now.isoformat(timespec="seconds")
            self._conn.execute(
                "UPDATE trigger_rules SET last_fired_ts = ? WHERE id = ?",
                (now_str, rule_id),
            )
            self._conn.commit()
            return True

        return False

    @staticmethod
    def _validate_direct_action(action: str) -> None:
        """Validate that action is a valid direct rule action.

        For direct rules, action must be a JSON string with exactly
        {"tool": str, "input": dict} keys.

        Raises:
            ValueError: if invalid
        """
        try:
            parsed = json.loads(action)
        except json.JSONDecodeError as exc:
            raise ValueError(f"action must be valid JSON for direct rules: {exc}") from exc

        if not isinstance(parsed, dict):
            raise ValueError("action must be a JSON object (dict) for direct rules")

        keys = set(parsed.keys())
        if keys != {"tool", "input"}:
            raise ValueError(
                f"direct rule action must have exactly keys 'tool' and 'input', got {sorted(keys)}"
            )

        tool = parsed["tool"]
        input_val = parsed["input"]

        if not isinstance(tool, str):
            raise ValueError(f"'tool' must be a string, got {type(tool).__name__}")

        if not isinstance(input_val, dict):
            raise ValueError(f"'input' must be a dict, got {type(input_val).__name__}")
