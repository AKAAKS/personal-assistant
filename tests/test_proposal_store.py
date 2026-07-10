"""Tests for ProposalStore — habit-proposal queue (add/list/get/accept/dismiss/has_open)."""

import json

import pytest

from jarvisd.db import connect
from jarvisd.proposal_store import ProposalStore


@pytest.fixture
def conn():
    """In-memory SQLite connection with schema."""
    c = connect(":memory:")
    yield c
    c.close()


@pytest.fixture
def store(conn):
    """ProposalStore instance."""
    return ProposalStore(conn)


class TestProposalStoreAdd:
    def test_add_returns_id_and_stores_row(self, store, conn):
        pid = store.add(
            signature="sig-1",
            kind="profile",
            payload={"fact": "Usually dims the living room around 7pm"},
            proposal_text="You often dim the living room around 7pm — want me to remember that?",
        )
        assert isinstance(pid, int)
        row = conn.execute("SELECT * FROM habit_proposals WHERE id=?", (pid,)).fetchone()
        assert row["signature"] == "sig-1"
        assert row["kind"] == "profile"
        assert json.loads(row["payload"]) == {"fact": "Usually dims the living room around 7pm"}
        assert row["proposal_text"].startswith("You often dim")
        assert row["status"] == "pending"
        assert row["created_ts"]  # stamped

    def test_add_json_encodes_payload(self, store):
        pid = store.add("sig-2", "rule", {"name": "x", "action": "y"}, "text")
        row = store.get(pid)
        assert json.loads(row["payload"]) == {"name": "x", "action": "y"}

    def test_add_duplicate_signature_returns_none(self, store):
        pid1 = store.add("dup-sig", "profile", {"fact": "a"}, "text a")
        assert pid1 is not None
        pid2 = store.add("dup-sig", "profile", {"fact": "b"}, "text b")
        assert pid2 is None
        # original row untouched
        rows = store.list_pending()
        assert len(rows) == 1
        assert json.loads(rows[0]["payload"]) == {"fact": "a"}

    def test_add_uses_now_param(self, store):
        pid = store.add("sig-now", "profile", {"fact": "a"}, "text", now="2026-01-01T00:00:00")
        row = store.get(pid)
        assert row["created_ts"] == "2026-01-01T00:00:00"


class TestProposalStoreListGet:
    def test_list_pending_empty_initially(self, store):
        assert store.list_pending() == []

    def test_list_pending_ordered_by_id(self, store):
        store.add("sig-a", "profile", {"fact": "a"}, "text a")
        store.add("sig-b", "profile", {"fact": "b"}, "text b")
        rows = store.list_pending()
        assert [r["signature"] for r in rows] == ["sig-a", "sig-b"]

    def test_list_pending_excludes_accepted_and_dismissed(self, store):
        pid1 = store.add("sig-a", "profile", {"fact": "a"}, "text a")
        pid2 = store.add("sig-b", "profile", {"fact": "b"}, "text b")
        store.add("sig-c", "profile", {"fact": "c"}, "text c")
        store.mark_accepted(pid1)
        store.mark_dismissed(pid2)
        rows = store.list_pending()
        assert [r["signature"] for r in rows] == ["sig-c"]

    def test_get_returns_row(self, store):
        pid = store.add("sig-1", "profile", {"fact": "a"}, "text")
        row = store.get(pid)
        assert row["id"] == pid
        assert row["signature"] == "sig-1"

    def test_get_missing_returns_none(self, store):
        assert store.get(9999) is None


class TestProposalStoreMarkTransitions:
    def test_mark_accepted_transitions_pending_to_accepted(self, store, conn):
        pid = store.add("sig-1", "profile", {"fact": "a"}, "text")
        assert store.mark_accepted(pid) is True
        row = conn.execute("SELECT status FROM habit_proposals WHERE id=?", (pid,)).fetchone()
        assert row["status"] == "accepted"

    def test_mark_accepted_false_when_not_pending(self, store):
        pid = store.add("sig-1", "profile", {"fact": "a"}, "text")
        assert store.mark_accepted(pid) is True
        assert store.mark_accepted(pid) is False  # already accepted

    def test_mark_accepted_false_for_unknown_id(self, store):
        assert store.mark_accepted(9999) is False

    def test_mark_dismissed_transitions_pending_to_dismissed(self, store, conn):
        pid = store.add("sig-1", "profile", {"fact": "a"}, "text")
        assert store.mark_dismissed(pid) is True
        row = conn.execute("SELECT status FROM habit_proposals WHERE id=?", (pid,)).fetchone()
        assert row["status"] == "dismissed"

    def test_mark_dismissed_false_when_not_pending(self, store):
        pid = store.add("sig-1", "profile", {"fact": "a"}, "text")
        assert store.mark_dismissed(pid) is True
        assert store.mark_dismissed(pid) is False  # already dismissed

    def test_mark_dismissed_false_for_unknown_id(self, store):
        assert store.mark_dismissed(9999) is False

    def test_mark_accepted_false_when_already_dismissed(self, store):
        pid = store.add("sig-1", "profile", {"fact": "a"}, "text")
        store.mark_dismissed(pid)
        assert store.mark_accepted(pid) is False


class TestProposalStoreHasOpen:
    def test_has_open_false_when_no_row(self, store):
        assert store.has_open("nope") is False

    def test_has_open_true_when_pending(self, store):
        store.add("sig-1", "profile", {"fact": "a"}, "text")
        assert store.has_open("sig-1") is True

    def test_has_open_true_when_accepted(self, store):
        pid = store.add("sig-1", "profile", {"fact": "a"}, "text")
        store.mark_accepted(pid)
        assert store.has_open("sig-1") is True

    def test_has_open_false_when_dismissed(self, store):
        pid = store.add("sig-1", "profile", {"fact": "a"}, "text")
        store.mark_dismissed(pid)
        assert store.has_open("sig-1") is False
