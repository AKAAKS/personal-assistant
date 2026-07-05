"""Tests for reminder and trigger rule tools."""

import sqlite3
import json
from datetime import datetime, timedelta
import pytest
from types import SimpleNamespace
from jarvisd.db import connect
from jarvisd.reminder_store import ReminderStore
from jarvisd.rules_store import RulesStore
from jarvisd.tools import reminders, rules


@pytest.fixture
def conn():
    """In-memory SQLite connection with schema."""
    c = connect(":memory:")
    yield c
    c.close()


@pytest.fixture
def reminder_store(conn):
    """ReminderStore instance."""
    return ReminderStore(conn)


@pytest.fixture
def rules_store(conn):
    """RulesStore instance."""
    return RulesStore(conn)


@pytest.fixture
def reminder_handlers(reminder_store):
    """Reminder tool handlers."""
    return reminders.handlers(reminder_store)


@pytest.fixture
def rule_handlers(rules_store):
    """Rule tool handlers."""
    return rules.handlers(rules_store)


class TestReminderTools:
    """Tests for reminder tool definitions and handlers."""

    def test_reminder_tools_defined(self):
        """Verify reminder tools are defined."""
        tool_names = [t["name"] for t in reminders.TOOLS]
        assert "set_reminder" in tool_names
        assert "list_reminders" in tool_names
        assert "cancel_reminder" in tool_names

    def test_reminder_tools_schema(self):
        """Verify reminder tool schemas are well-formed."""
        for tool in reminders.TOOLS:
            assert "name" in tool
            assert "description" in tool
            assert "input_schema" in tool
            assert tool["input_schema"]["type"] == "object"

    def test_set_reminder_handler_valid(self, reminder_handlers, reminder_store):
        """Test set_reminder handler with valid input."""
        due = (datetime.now() + timedelta(hours=1)).isoformat()
        result = reminder_handlers["set_reminder"]({
            "text": "Take a break",
            "due": due,
            "recurrence": "none"
        })
        assert "Reminder" in result
        assert "set for" in result
        assert due in result

    def test_set_reminder_handler_no_recurrence(self, reminder_handlers, reminder_store):
        """Test set_reminder defaults recurrence to 'none'."""
        due = (datetime.now() + timedelta(hours=1)).isoformat()
        result = reminder_handlers["set_reminder"]({
            "text": "Buy milk",
            "due": due
        })
        assert "Reminder" in result

    def test_set_reminder_handler_invalid_due(self, reminder_handlers):
        """Test set_reminder with invalid ISO datetime."""
        result = reminder_handlers["set_reminder"]({
            "text": "Test",
            "due": "not-a-datetime",
            "recurrence": "none"
        })
        assert isinstance(result, str)
        # Should contain error message
        assert "error" in result.lower() or "invalid" in result.lower() or "datetime" in result.lower()

    def test_set_reminder_handler_invalid_recurrence(self, reminder_handlers):
        """Test set_reminder with invalid recurrence."""
        due = (datetime.now() + timedelta(hours=1)).isoformat()
        result = reminder_handlers["set_reminder"]({
            "text": "Test",
            "due": due,
            "recurrence": "invalid_recurrence"
        })
        assert isinstance(result, str)
        assert "error" in result.lower() or "invalid" in result.lower() or "recurrence" in result.lower()

    def test_list_reminders_handler_empty(self, reminder_handlers):
        """Test list_reminders with no reminders."""
        result = reminder_handlers["list_reminders"]({})
        assert "No active reminders" in result

    def test_list_reminders_handler_with_reminders(self, reminder_handlers, reminder_store):
        """Test list_reminders with active reminders."""
        due1 = (datetime.now() + timedelta(hours=1)).isoformat()
        due2 = (datetime.now() + timedelta(hours=2)).isoformat()

        reminder_store.add("First task", due1, "none")
        reminder_store.add("Second task", due2, "none")

        result = reminder_handlers["list_reminders"]({})
        assert "First task" in result
        assert "Second task" in result
        assert "1." in result or "#1" in result  # numbering

    def test_cancel_reminder_handler_valid(self, reminder_handlers, reminder_store):
        """Test cancel_reminder with valid ID."""
        due = (datetime.now() + timedelta(hours=1)).isoformat()
        reminder_id = reminder_store.add("Task", due, "none")

        result = reminder_handlers["cancel_reminder"]({"reminder_id": reminder_id})
        assert "cancel" in result.lower() or "removed" in result.lower() or "done" in result.lower()

    def test_cancel_reminder_handler_not_found(self, reminder_handlers):
        """Test cancel_reminder with nonexistent ID."""
        result = reminder_handlers["cancel_reminder"]({"reminder_id": 999})
        assert "not found" in result.lower() or "no reminder" in result.lower()


class TestRuleTools:
    """Tests for rule tool definitions and handlers."""

    def test_rule_tools_defined(self):
        """Verify rule tools are defined."""
        tool_names = [t["name"] for t in rules.TOOLS]
        assert "add_trigger_rule" in tool_names
        assert "list_trigger_rules" in tool_names
        assert "delete_trigger_rule" in tool_names

    def test_rule_tools_schema(self):
        """Verify rule tool schemas are well-formed."""
        for tool in rules.TOOLS:
            assert "name" in tool
            assert "description" in tool
            assert "input_schema" in tool
            assert tool["input_schema"]["type"] == "object"

    def test_add_trigger_rule_handler_direct_valid(self, rule_handlers, rules_store):
        """Test add_trigger_rule handler with valid direct action."""
        action = json.dumps({"tool": "control_light", "input": {"entity_id": "light.hall"}})
        result = rule_handlers["add_trigger_rule"]({
            "name": "Motion detector",
            "event_type": "state_changed",
            "kind": "direct",
            "action": action,
            "entity_id": "motion.hall",
            "cooldown_sec": 60
        })
        assert "Rule" in result
        assert "Motion detector" in result
        assert "added" in result

    def test_add_trigger_rule_handler_smart_valid(self, rule_handlers):
        """Test add_trigger_rule handler with valid smart action."""
        result = rule_handlers["add_trigger_rule"]({
            "name": "Motion notifier",
            "event_type": "state_changed",
            "kind": "smart",
            "action": "Tell the user that motion was detected",
            "entity_id": "motion.hall",
            "cooldown_sec": 300
        })
        assert "Rule" in result
        assert "Motion notifier" in result
        assert "added" in result

    def test_add_trigger_rule_handler_direct_invalid_json(self, rule_handlers):
        """Test add_trigger_rule with invalid JSON for direct rule."""
        result = rule_handlers["add_trigger_rule"]({
            "name": "Bad rule",
            "event_type": "state_changed",
            "kind": "direct",
            "action": "not json"
        })
        assert isinstance(result, str)
        assert "error" in result.lower() or "invalid" in result.lower()

    def test_add_trigger_rule_handler_direct_missing_tool(self, rule_handlers):
        """Test add_trigger_rule with missing 'tool' key."""
        action = json.dumps({"input": {}})
        result = rule_handlers["add_trigger_rule"]({
            "name": "Bad rule",
            "event_type": "state_changed",
            "kind": "direct",
            "action": action
        })
        assert isinstance(result, str)
        assert "error" in result.lower()

    def test_add_trigger_rule_handler_invalid_kind(self, rule_handlers):
        """Test add_trigger_rule with invalid kind."""
        result = rule_handlers["add_trigger_rule"]({
            "name": "Bad rule",
            "event_type": "state_changed",
            "kind": "invalid_kind",
            "action": "some action"
        })
        assert isinstance(result, str)
        assert "error" in result.lower()

    def test_list_trigger_rules_handler_empty(self, rule_handlers):
        """Test list_trigger_rules with no rules."""
        result = rule_handlers["list_trigger_rules"]({})
        assert "No trigger rules" in result or "no rules" in result.lower()

    def test_list_trigger_rules_handler_with_rules(self, rule_handlers, rules_store):
        """Test list_trigger_rules with active rules."""
        action = json.dumps({"tool": "test", "input": {}})
        rules_store.add("Rule 1", "state_changed", "direct", action)
        rules_store.add("Rule 2", "state_changed", "smart", "action text")

        result = rule_handlers["list_trigger_rules"]({})
        assert "Rule 1" in result
        assert "Rule 2" in result
        assert "1." in result or "#1" in result  # numbering

    def test_delete_trigger_rule_handler_valid(self, rule_handlers, rules_store):
        """Test delete_trigger_rule with valid ID."""
        action = json.dumps({"tool": "test", "input": {}})
        rule_id = rules_store.add("Test rule", "state_changed", "direct", action)

        result = rule_handlers["delete_trigger_rule"]({"rule_id": rule_id})
        assert "delete" in result.lower() or "removed" in result.lower() or "done" in result.lower()

    def test_delete_trigger_rule_handler_not_found(self, rule_handlers):
        """Test delete_trigger_rule with nonexistent ID."""
        result = rule_handlers["delete_trigger_rule"]({"rule_id": 999})
        assert "not found" in result.lower() or "no rule" in result.lower()


class TestToolSecurity:
    """Test that tool descriptions mention security/examples."""

    def test_add_trigger_rule_description_has_examples(self):
        """Verify add_trigger_rule description includes examples."""
        tool = [t for t in rules.TOOLS if t["name"] == "add_trigger_rule"][0]
        description = tool["description"].lower()
        # Should mention both direct and smart
        assert "direct" in description
        assert "smart" in description
        # Should mention JSON or tool/input
        assert "json" in description or "tool" in description or "action" in description
