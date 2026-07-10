"""Tests for habit-proposal tools: list_proposals / accept_proposal / dismiss_proposal."""

from types import SimpleNamespace

import pytest

from jarvisd.db import connect
from jarvisd.proposal_store import ProposalStore
from jarvisd.tools import proposals


@pytest.fixture
def conn():
    """In-memory SQLite connection with schema."""
    c = connect(":memory:")
    yield c
    c.close()


@pytest.fixture
def proposal_store(conn):
    """ProposalStore instance."""
    return ProposalStore(conn)


@pytest.fixture
def profile_spy():
    """Spy profile_store recording append() calls."""
    spy = SimpleNamespace()
    spy.calls = []

    def append(fact):
        spy.calls.append(fact)
        return f"Remembered: {fact}"

    spy.append = append
    return spy


@pytest.fixture
def rules_spy():
    """Spy rules_store recording add() calls."""
    spy = SimpleNamespace()
    spy.calls = []

    def add(**kwargs):
        spy.calls.append(kwargs)
        return 1

    spy.add = add
    return spy


class TestProposalToolDefs:
    """Tool definitions are well-formed."""

    def test_tools_defined(self):
        tool_names = [t["name"] for t in proposals.TOOLS]
        assert "list_proposals" in tool_names
        assert "accept_proposal" in tool_names
        assert "dismiss_proposal" in tool_names

    def test_tools_schema(self):
        for tool in proposals.TOOLS:
            assert "name" in tool
            assert "description" in tool
            assert "input_schema" in tool
            assert tool["input_schema"]["type"] == "object"

    def test_list_proposals_has_no_required_input(self):
        tool = next(t for t in proposals.TOOLS if t["name"] == "list_proposals")
        assert tool["input_schema"].get("required", []) == []

    def test_accept_proposal_requires_id(self):
        tool = next(t for t in proposals.TOOLS if t["name"] == "accept_proposal")
        assert "id" in tool["input_schema"]["required"]
        assert tool["input_schema"]["properties"]["id"]["type"] == "integer"

    def test_dismiss_proposal_requires_id(self):
        tool = next(t for t in proposals.TOOLS if t["name"] == "dismiss_proposal")
        assert "id" in tool["input_schema"]["required"]
        assert tool["input_schema"]["properties"]["id"]["type"] == "integer"


class TestListProposals:
    def test_empty(self, proposal_store):
        handlers = proposals.handlers(proposal_store=proposal_store)
        result = handlers["list_proposals"]({})
        assert result == "No pending suggestions."

    def test_formats_enumerated_list(self, proposal_store):
        pid1 = proposal_store.add("sig1", "profile", {"fact": "likes tea"}, "You seem to like tea in the morning.")
        pid2 = proposal_store.add("sig2", "profile", {"fact": "likes coffee"}, "You seem to like coffee at night.")

        handlers = proposals.handlers(proposal_store=proposal_store)
        result = handlers["list_proposals"]({})

        lines = result.splitlines()
        assert lines[0] == f"1) You seem to like tea in the morning. [#{pid1}]"
        assert lines[1] == f"2) You seem to like coffee at night. [#{pid2}]"

    def test_only_pending_shown(self, proposal_store):
        pid1 = proposal_store.add("sig1", "profile", {"fact": "a"}, "Suggestion A")
        pid2 = proposal_store.add("sig2", "profile", {"fact": "b"}, "Suggestion B")
        proposal_store.mark_dismissed(pid1)

        handlers = proposals.handlers(proposal_store=proposal_store)
        result = handlers["list_proposals"]({})

        assert "Suggestion A" not in result
        assert f"Suggestion B [#{pid2}]" in result

    def test_missing_store_errors(self):
        handlers = proposals.handlers()
        result = handlers["list_proposals"]({})
        assert result == "Error: proposals not available."


class TestAcceptProposal:
    def test_accept_profile_proposal_writes_and_marks_accepted(self, proposal_store, profile_spy):
        pid = proposal_store.add("sig1", "profile", {"fact": "likes tea"}, "You seem to like tea.")
        handlers = proposals.handlers(proposal_store=proposal_store, profile_store=profile_spy)

        result = handlers["accept_proposal"]({"id": pid})

        assert isinstance(result, str)
        assert not result.startswith("Error")
        assert profile_spy.calls == ["likes tea"]

        row = proposal_store.get(pid)
        assert row["status"] == "accepted"

    def test_second_accept_of_same_id_errors(self, proposal_store, profile_spy):
        pid = proposal_store.add("sig1", "profile", {"fact": "likes tea"}, "You seem to like tea.")
        handlers = proposals.handlers(proposal_store=proposal_store, profile_store=profile_spy)

        first = handlers["accept_proposal"]({"id": pid})
        assert not first.startswith("Error")

        second = handlers["accept_proposal"]({"id": pid})
        assert second == f"Error: no pending suggestion with id {pid}."
        # Must not have written a second time
        assert profile_spy.calls == ["likes tea"]

    def test_accept_rule_proposal_calls_rules_store_with_stored_kwargs(self, proposal_store, rules_spy):
        payload = {
            "name": "Turn off desk lamp at night",
            "event_type": "state_changed",
            "kind": "direct",
            "action": '{"tool": "control_light", "input": {"entity_id": "light.desk", "action": "turn_off"}}',
            "entity_id": "light.desk",
            "to_state": "on",
            "cooldown_sec": 3600,
        }
        pid = proposal_store.add("sig-rule", "rule", payload, "Turn off the desk lamp automatically at night?")

        handlers = proposals.handlers(proposal_store=proposal_store, rules_store=rules_spy)
        result = handlers["accept_proposal"]({"id": pid})

        assert isinstance(result, str)
        assert not result.startswith("Error")
        assert rules_spy.calls == [payload]

        row = proposal_store.get(pid)
        assert row["status"] == "accepted"

    def test_accept_rule_proposal_when_store_raises_returns_error_string(self, proposal_store):
        # rules_store.add can raise ValueError (e.g. bad kind); handler must
        # never propagate it.
        payload = {"name": "bad", "event_type": "state_changed", "kind": "bogus", "action": "{}"}
        pid = proposal_store.add("sig-bad-rule", "rule", payload, "A bogus rule suggestion")

        def raising_add(**kwargs):
            raise ValueError(f"kind must be 'direct' or 'smart', got {kwargs.get('kind')!r}")

        rules_store = SimpleNamespace(add=raising_add)
        handlers = proposals.handlers(proposal_store=proposal_store, rules_store=rules_store)

        result = handlers["accept_proposal"]({"id": pid})

        assert isinstance(result, str)
        assert result.startswith("Error")

        # Proposal should remain pending since the write failed
        row = proposal_store.get(pid)
        assert row["status"] == "pending"

    def test_accept_unknown_id_errors(self, proposal_store, profile_spy):
        handlers = proposals.handlers(proposal_store=proposal_store, profile_store=profile_spy)
        result = handlers["accept_proposal"]({"id": 999})
        assert result == "Error: no pending suggestion with id 999."

    def test_accept_missing_proposal_store_errors(self):
        handlers = proposals.handlers()
        result = handlers["accept_proposal"]({"id": 1})
        assert result == "Error: proposals not available."

    def test_accept_profile_missing_profile_store_errors(self, proposal_store):
        pid = proposal_store.add("sig1", "profile", {"fact": "likes tea"}, "You seem to like tea.")
        handlers = proposals.handlers(proposal_store=proposal_store)
        result = handlers["accept_proposal"]({"id": pid})
        assert result == "Error: profile not available."
        # Proposal stays pending
        row = proposal_store.get(pid)
        assert row["status"] == "pending"

    def test_accept_rule_missing_rules_store_errors(self, proposal_store):
        payload = {"name": "x", "event_type": "state_changed", "kind": "direct", "action": "{}"}
        pid = proposal_store.add("sig-rule2", "rule", payload, "A rule suggestion")
        handlers = proposals.handlers(proposal_store=proposal_store)
        result = handlers["accept_proposal"]({"id": pid})
        assert result == "Error: rules not available."
        row = proposal_store.get(pid)
        assert row["status"] == "pending"

    def test_accept_unknown_kind_errors(self, proposal_store):
        pid = proposal_store.add("sig-weird", "weird_kind", {"x": 1}, "A weird proposal")
        handlers = proposals.handlers(proposal_store=proposal_store)
        result = handlers["accept_proposal"]({"id": pid})
        assert isinstance(result, str)
        assert result.startswith("Error")
        row = proposal_store.get(pid)
        assert row["status"] == "pending"

    def test_accept_non_int_id_returns_error_string(self, proposal_store, profile_spy):
        handlers = proposals.handlers(proposal_store=proposal_store, profile_store=profile_spy)
        result = handlers["accept_proposal"]({"id": "abc"})
        assert isinstance(result, str)
        assert result.startswith("Error")

    def test_accept_missing_id_returns_error_string(self, proposal_store, profile_spy):
        handlers = proposals.handlers(proposal_store=proposal_store, profile_store=profile_spy)
        result = handlers["accept_proposal"]({})
        assert isinstance(result, str)
        assert result.startswith("Error")

    def test_accept_bool_id_rejected(self, proposal_store, profile_spy):
        # isinstance(True, int) is True in Python; must still be rejected as
        # this is clearly not a proposal id.
        handlers = proposals.handlers(proposal_store=proposal_store, profile_store=profile_spy)
        result = handlers["accept_proposal"]({"id": True})
        assert isinstance(result, str)
        assert result.startswith("Error")


class TestDismissProposal:
    def test_dismiss_marks_dismissed(self, proposal_store):
        pid = proposal_store.add("sig1", "profile", {"fact": "likes tea"}, "You seem to like tea.")
        handlers = proposals.handlers(proposal_store=proposal_store)

        result = handlers["dismiss_proposal"]({"id": pid})

        assert isinstance(result, str)
        assert not result.startswith("Error")
        row = proposal_store.get(pid)
        assert row["status"] == "dismissed"

    def test_dismiss_unknown_id_errors(self, proposal_store):
        handlers = proposals.handlers(proposal_store=proposal_store)
        result = handlers["dismiss_proposal"]({"id": 999})
        assert result == "Error: no pending suggestion with id 999."

    def test_dismiss_already_dismissed_errors(self, proposal_store):
        pid = proposal_store.add("sig1", "profile", {"fact": "likes tea"}, "You seem to like tea.")
        handlers = proposals.handlers(proposal_store=proposal_store)
        handlers["dismiss_proposal"]({"id": pid})

        result = handlers["dismiss_proposal"]({"id": pid})
        assert result == f"Error: no pending suggestion with id {pid}."

    def test_dismiss_missing_store_errors(self):
        handlers = proposals.handlers()
        result = handlers["dismiss_proposal"]({"id": 1})
        assert result == "Error: proposals not available."

    def test_dismiss_non_int_id_returns_error_string(self, proposal_store):
        handlers = proposals.handlers(proposal_store=proposal_store)
        result = handlers["dismiss_proposal"]({"id": "abc"})
        assert isinstance(result, str)
        assert result.startswith("Error")

    def test_dismiss_missing_id_returns_error_string(self, proposal_store):
        handlers = proposals.handlers(proposal_store=proposal_store)
        result = handlers["dismiss_proposal"]({})
        assert isinstance(result, str)
        assert result.startswith("Error")
