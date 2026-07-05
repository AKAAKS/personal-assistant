"""Tests for RulesStore — trigger rule storage and matching."""

import sqlite3
import json
from datetime import datetime, timedelta
import pytest
from jarvisd.db import connect
from jarvisd.rules_store import RulesStore


@pytest.fixture
def conn():
    """In-memory SQLite connection with schema."""
    c = connect(":memory:")
    yield c
    c.close()


@pytest.fixture
def store(conn):
    """RulesStore instance."""
    return RulesStore(conn)


class TestRulesStoreAdd:
    """Test adding rules with validation."""

    def test_add_direct_rule_valid(self, store):
        """Add a direct rule with valid JSON action."""
        action = json.dumps({"tool": "control_light", "input": {"entity_id": "light.hall", "action": "turn_on"}})
        rule_id = store.add(
            "Hall light on motion",
            "state_changed",
            "direct",
            action,
            entity_id="motion.hall",
            to_state="on",
            cooldown_sec=60
        )
        assert isinstance(rule_id, int)
        assert rule_id > 0

    def test_add_smart_rule_valid(self, store):
        """Add a smart rule with free-text action."""
        rule_id = store.add(
            "Notify about motion",
            "state_changed",
            "smart",
            "Send a notification that motion was detected in the hall",
            entity_id="motion.hall",
            cooldown_sec=300
        )
        assert isinstance(rule_id, int)
        assert rule_id > 0

    def test_add_rule_direct_invalid_json(self, store):
        """Direct rule with invalid JSON action raises ValueError."""
        with pytest.raises(ValueError, match="must be valid JSON"):
            store.add(
                "Bad rule",
                "state_changed",
                "direct",
                "not json",
            )

    def test_add_rule_direct_missing_tool_key(self, store):
        """Direct rule without 'tool' key raises ValueError."""
        action = json.dumps({"input": {"key": "value"}})
        with pytest.raises(ValueError, match="'tool'"):
            store.add(
                "Bad rule",
                "state_changed",
                "direct",
                action,
            )

    def test_add_rule_direct_missing_input_key(self, store):
        """Direct rule without 'input' key raises ValueError."""
        action = json.dumps({"tool": "control_light"})
        with pytest.raises(ValueError, match="'input'"):
            store.add(
                "Bad rule",
                "state_changed",
                "direct",
                action,
            )

    def test_add_rule_direct_extra_keys(self, store):
        """Direct rule with extra keys raises ValueError."""
        action = json.dumps({
            "tool": "control_light",
            "input": {"entity_id": "light.hall"},
            "extra": "not allowed"
        })
        with pytest.raises(ValueError, match="exactly"):
            store.add(
                "Bad rule",
                "state_changed",
                "direct",
                action,
            )

    def test_add_rule_direct_input_not_dict(self, store):
        """Direct rule with non-dict input raises ValueError."""
        action = json.dumps({"tool": "control_light", "input": "not a dict"})
        with pytest.raises(ValueError, match="dict"):
            store.add(
                "Bad rule",
                "state_changed",
                "direct",
                action,
            )

    def test_add_rule_direct_tool_not_string(self, store):
        """Direct rule with non-string tool raises ValueError."""
        action = json.dumps({"tool": 123, "input": {}})
        with pytest.raises(ValueError, match="string"):
            store.add(
                "Bad rule",
                "state_changed",
                "direct",
                action,
            )

    def test_add_rule_invalid_kind(self, store):
        """Rule with invalid kind raises ValueError."""
        with pytest.raises(ValueError, match="kind"):
            store.add(
                "Bad rule",
                "state_changed",
                "invalid_kind",
                "some action",
            )

    def test_add_rule_defaults(self, store):
        """Rule added with defaults."""
        action = json.dumps({"tool": "test", "input": {}})
        rule_id = store.add(
            "Test rule",
            "state_changed",
            "direct",
            action,
        )
        # Retrieve and check defaults
        rows = store.list_all()
        rule = [r for r in rows if r["id"] == rule_id][0]
        assert rule["entity_id"] is None
        assert rule["to_state"] is None
        assert rule["cooldown_sec"] == 300
        assert rule["enabled"] == 1


class TestRulesStoreList:
    """Test listing rules."""

    def test_list_enabled(self, store):
        """List only enabled rules."""
        action = json.dumps({"tool": "test", "input": {}})
        id1 = store.add("Rule 1", "state_changed", "direct", action)
        id2 = store.add("Rule 2", "state_changed", "direct", action)

        # Disable second rule
        store._conn.execute("UPDATE trigger_rules SET enabled = 0 WHERE id = ?", (id2,))
        store._conn.commit()

        enabled = store.list_enabled()
        enabled_ids = [r["id"] for r in enabled]
        assert id1 in enabled_ids
        assert id2 not in enabled_ids

    def test_list_all(self, store):
        """List all rules regardless of enabled status."""
        action = json.dumps({"tool": "test", "input": {}})
        id1 = store.add("Rule 1", "state_changed", "direct", action)
        id2 = store.add("Rule 2", "state_changed", "direct", action)

        # Disable second rule
        store._conn.execute("UPDATE trigger_rules SET enabled = 0 WHERE id = ?", (id2,))
        store._conn.commit()

        all_rules = store.list_all()
        all_ids = [r["id"] for r in all_rules]
        assert id1 in all_ids
        assert id2 in all_ids


class TestRulesStoreMatches:
    """Test event matching against rules."""

    def test_matches_event_type_only(self, store):
        """Match rules on event_type alone."""
        action = json.dumps({"tool": "test", "input": {}})
        rule_id = store.add("Test rule", "state_changed", "direct", action)

        matches = store.matches({"event_type": "state_changed", "entity_id": None, "to_state": None})
        assert len(matches) == 1
        assert matches[0]["id"] == rule_id

    def test_matches_event_type_mismatch(self, store):
        """No match when event_type doesn't match."""
        action = json.dumps({"tool": "test", "input": {}})
        store.add("Test rule", "state_changed", "direct", action)

        matches = store.matches({"event_type": "service_called", "entity_id": None, "to_state": None})
        assert len(matches) == 0

    def test_matches_with_entity_id_filter(self, store):
        """Match rules filtered by entity_id."""
        action = json.dumps({"tool": "test", "input": {}})
        id1 = store.add("Motion rule", "state_changed", "direct", action, entity_id="motion.hall")
        id2 = store.add("General rule", "state_changed", "direct", action)

        # Event for motion.hall
        matches = store.matches({"event_type": "state_changed", "entity_id": "motion.hall", "to_state": None})
        match_ids = [r["id"] for r in matches]
        assert id1 in match_ids
        assert id2 in match_ids  # general rules (no entity_id filter) should still match

        # Event for different entity
        matches = store.matches({"event_type": "state_changed", "entity_id": "motion.lounge", "to_state": None})
        match_ids = [r["id"] for r in matches]
        assert id1 not in match_ids
        assert id2 in match_ids  # general rule still matches

    def test_matches_with_to_state_filter(self, store):
        """Match rules filtered by to_state."""
        action = json.dumps({"tool": "test", "input": {}})
        id1 = store.add("On rule", "state_changed", "direct", action, to_state="on")
        id2 = store.add("General rule", "state_changed", "direct", action)

        # Event with to_state="on"
        matches = store.matches({"event_type": "state_changed", "entity_id": None, "to_state": "on"})
        match_ids = [r["id"] for r in matches]
        assert id1 in match_ids
        assert id2 in match_ids

        # Event with to_state="off"
        matches = store.matches({"event_type": "state_changed", "entity_id": None, "to_state": "off"})
        match_ids = [r["id"] for r in matches]
        assert id1 not in match_ids
        assert id2 in match_ids

    def test_matches_with_entity_and_state_filters(self, store):
        """Match rules with both entity_id and to_state filters."""
        action = json.dumps({"tool": "test", "input": {}})
        id1 = store.add("Specific", "state_changed", "direct", action, entity_id="motion.hall", to_state="on")
        id2 = store.add("Entity only", "state_changed", "direct", action, entity_id="motion.hall")
        id3 = store.add("State only", "state_changed", "direct", action, to_state="on")
        id4 = store.add("General", "state_changed", "direct", action)

        # Event matches all
        matches = store.matches({"event_type": "state_changed", "entity_id": "motion.hall", "to_state": "on"})
        match_ids = [r["id"] for r in matches]
        assert set(match_ids) == {id1, id2, id3, id4}

    def test_matches_only_enabled_rules(self, store):
        """Matches only returns enabled rules."""
        action = json.dumps({"tool": "test", "input": {}})
        id1 = store.add("Enabled", "state_changed", "direct", action)
        id2 = store.add("Disabled", "state_changed", "direct", action)

        store._conn.execute("UPDATE trigger_rules SET enabled = 0 WHERE id = ?", (id2,))
        store._conn.commit()

        matches = store.matches({"event_type": "state_changed", "entity_id": None, "to_state": None})
        match_ids = [r["id"] for r in matches]
        assert id1 in match_ids
        assert id2 not in match_ids


class TestRulesStoreCooldown:
    """Test cooldown checking and stamping."""

    def test_check_and_stamp_cooldown_first_fire(self, store):
        """First fire always allowed."""
        action = json.dumps({"tool": "test", "input": {}})
        rule_id = store.add("Test rule", "state_changed", "direct", action, cooldown_sec=60)

        now = datetime.now()
        allowed = store.check_and_stamp_cooldown(rule_id, now)
        assert allowed is True

        # Verify last_fired_ts was stamped
        rule = store._conn.execute("SELECT last_fired_ts FROM trigger_rules WHERE id = ?", (rule_id,)).fetchone()
        assert rule["last_fired_ts"] is not None

    def test_check_and_stamp_cooldown_blocks_immediate_refire(self, store):
        """Immediate refire is blocked."""
        action = json.dumps({"tool": "test", "input": {}})
        rule_id = store.add("Test rule", "state_changed", "direct", action, cooldown_sec=60)

        now = datetime.now()
        store.check_and_stamp_cooldown(rule_id, now)

        # Attempt refire immediately
        now_plus_1 = now + timedelta(seconds=1)
        allowed = store.check_and_stamp_cooldown(rule_id, now_plus_1)
        assert allowed is False

    def test_check_and_stamp_cooldown_allows_after_elapsed(self, store):
        """Fire allowed after cooldown period has elapsed."""
        action = json.dumps({"tool": "test", "input": {}})
        rule_id = store.add("Test rule", "state_changed", "direct", action, cooldown_sec=60)

        now = datetime.now()
        store.check_and_stamp_cooldown(rule_id, now)

        # Refire after cooldown elapsed
        now_plus_61 = now + timedelta(seconds=61)
        allowed = store.check_and_stamp_cooldown(rule_id, now_plus_61)
        assert allowed is True

    def test_check_and_stamp_cooldown_exact_boundary(self, store):
        """Fire allowed exactly at cooldown boundary."""
        action = json.dumps({"tool": "test", "input": {}})
        rule_id = store.add("Test rule", "state_changed", "direct", action, cooldown_sec=60)

        now = datetime.now()
        store.check_and_stamp_cooldown(rule_id, now)

        # Refire exactly at cooldown boundary
        now_plus_60 = now + timedelta(seconds=60)
        allowed = store.check_and_stamp_cooldown(rule_id, now_plus_60)
        assert allowed is True


class TestRulesStoreDelete:
    """Test rule deletion."""

    def test_delete_existing_rule(self, store):
        """Delete an existing rule."""
        action = json.dumps({"tool": "test", "input": {}})
        rule_id = store.add("Test rule", "state_changed", "direct", action)

        result = store.delete(rule_id)
        assert result is True

        # Rule should be gone
        all_rules = store.list_all()
        assert not any(r["id"] == rule_id for r in all_rules)

    def test_delete_nonexistent_rule(self, store):
        """Delete returns False for nonexistent rule."""
        result = store.delete(999)
        assert result is False


class TestRulesStoreIntegration:
    """Integration tests combining multiple operations."""

    def test_full_workflow(self, store):
        """Complete workflow: add, match, check cooldown, delete."""
        action = json.dumps({"tool": "control_light", "input": {"entity_id": "light.hall"}})

        # Add rule
        rule_id = store.add(
            "Motion detection",
            "state_changed",
            "direct",
            action,
            entity_id="motion.hall",
            to_state="on",
            cooldown_sec=30
        )

        # Check rule is listed
        enabled = store.list_enabled()
        assert any(r["id"] == rule_id for r in enabled)

        # Simulate event
        event = {"event_type": "state_changed", "entity_id": "motion.hall", "to_state": "on"}
        matches = store.matches(event)
        assert len(matches) > 0
        matched_rule = [r for r in matches if r["id"] == rule_id][0]

        # Check cooldown - first fire allowed
        now = datetime.now()
        can_fire = store.check_and_stamp_cooldown(rule_id, now)
        assert can_fire is True

        # Immediate refire blocked
        can_fire = store.check_and_stamp_cooldown(rule_id, now + timedelta(seconds=5))
        assert can_fire is False

        # Fire allowed after cooldown
        can_fire = store.check_and_stamp_cooldown(rule_id, now + timedelta(seconds=31))
        assert can_fire is True

        # Delete rule
        deleted = store.delete(rule_id)
        assert deleted is True

        # Rule no longer listed
        enabled = store.list_enabled()
        assert not any(r["id"] == rule_id for r in enabled)
