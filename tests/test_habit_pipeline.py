"""Tests for the habit scan pipeline — mine -> phrase once -> queue.

Uses a REAL ProposalStore backed by an in-memory SQLite DB so the
has_open/add integration is genuinely exercised, plus a synthetic
audit.jsonl with two distinct habits (two candidates).
"""

import json
from datetime import datetime, timezone

import pytest

from jarvisd.db import connect
from jarvisd.habit_pipeline import run_habit_scan
from jarvisd.proposal_store import ProposalStore

NOW = datetime(2026, 7, 10, 12, 0, 0, tzinfo=timezone.utc)


def _entry(ts, tool, input, verdict="allowed", reason="ok"):
    return {
        "ts": ts.isoformat(),
        "tool": tool,
        "input": input,
        "verdict": verdict,
        "reason": reason,
    }


def _write_audit(path, entries):
    with open(path, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


def _two_habit_entries():
    """Two distinct, independently-qualifying habits: bedroom light + kitchen switch."""
    entries = []
    for day in (1, 2, 3, 4):
        entries.append(
            _entry(
                datetime(2026, 7, day, 12, 0, 0, tzinfo=timezone.utc),
                "control_light",
                {"entity_id": "light.bedroom", "action": "turn_on"},
            )
        )
        entries.append(
            _entry(
                datetime(2026, 7, day, 2, 0, 0, tzinfo=timezone.utc),
                "control_switch",
                {"entity_id": "switch.kettle", "action": "turn_on"},
            )
        )
    return entries


@pytest.fixture
def conn():
    c = connect(":memory:")
    yield c
    c.close()


@pytest.fixture
def store(conn):
    return ProposalStore(conn)


class SpyPhraser:
    def __init__(self, raise_for_tool=None):
        self.calls = []
        self.raise_for_tool = raise_for_tool

    def __call__(self, candidate):
        self.calls.append(candidate)
        if self.raise_for_tool and candidate.evidence.get("tool") == self.raise_for_tool:
            raise RuntimeError("phraser boom")
        return f"proposal text for {candidate.signature}"


class TestRunHabitScan:
    def test_fresh_scan_stores_n_proposals_and_calls_phraser_n_times(self, tmp_path, store):
        audit_path = tmp_path / "audit.jsonl"
        _write_audit(audit_path, _two_habit_entries())
        phraser = SpyPhraser()

        count = run_habit_scan(str(audit_path), store, phraser, now=NOW)

        assert count == 2
        assert len(phraser.calls) == 2
        assert len(store.list_pending()) == 2

    def test_second_scan_is_idempotent_zero_adds_zero_phraser_calls(self, tmp_path, store):
        audit_path = tmp_path / "audit.jsonl"
        _write_audit(audit_path, _two_habit_entries())

        first_phraser = SpyPhraser()
        first_count = run_habit_scan(str(audit_path), store, first_phraser, now=NOW)
        assert first_count == 2

        second_phraser = SpyPhraser()
        second_count = run_habit_scan(str(audit_path), store, second_phraser, now=NOW)

        assert second_count == 0
        assert len(second_phraser.calls) == 0
        assert len(store.list_pending()) == 2  # unchanged

    def test_phraser_raising_on_one_candidate_does_not_abort_the_rest(self, tmp_path, store):
        audit_path = tmp_path / "audit.jsonl"
        _write_audit(audit_path, _two_habit_entries())
        phraser = SpyPhraser(raise_for_tool="control_switch")

        count = run_habit_scan(str(audit_path), store, phraser, now=NOW)

        assert count == 1
        assert len(phraser.calls) == 2  # both attempted
        pending = store.list_pending()
        assert len(pending) == 1
        assert pending[0]["proposal_text"] != ""

    def test_empty_audit_returns_zero_and_never_calls_phraser(self, tmp_path, store):
        audit_path = tmp_path / "audit.jsonl"
        _write_audit(audit_path, [])
        phraser = SpyPhraser()

        count = run_habit_scan(str(audit_path), store, phraser, now=NOW)

        assert count == 0
        assert len(phraser.calls) == 0
        assert store.list_pending() == []

    def test_missing_audit_file_returns_zero_and_never_calls_phraser(self, tmp_path, store):
        audit_path = tmp_path / "does_not_exist.jsonl"
        phraser = SpyPhraser()

        count = run_habit_scan(str(audit_path), store, phraser, now=NOW)

        assert count == 0
        assert len(phraser.calls) == 0

    def test_dismissed_proposal_is_still_mined_as_a_candidate(self, tmp_path, store):
        """has_open() is False for a dismissed signature (dismissal doesn't block

        re-mining at the pipeline level), so the pipeline still attempts to
        phrase+add it. NOTE: ProposalStore.add() enforces UNIQUE(signature) at
        the DB layer regardless of status, so a dismissed row's signature
        cannot be re-inserted; add() returns None and the pipeline correctly
        counts it as not-new (per brief: "If add() returns None ... count it
        as not-new"). Full re-surfacing of dismissed habits would need a
        store-level change (out of scope for Task 3 — see task-3-report.md).
        """
        audit_path = tmp_path / "audit.jsonl"
        _write_audit(audit_path, _two_habit_entries())

        first_phraser = SpyPhraser()
        run_habit_scan(str(audit_path), store, first_phraser, now=NOW)

        pending = store.list_pending()
        assert len(pending) == 2
        dismissed_sig = pending[0]["signature"]
        store.mark_dismissed(pending[0]["id"])
        assert store.has_open(dismissed_sig) is False

        second_phraser = SpyPhraser()
        count = run_habit_scan(str(audit_path), store, second_phraser, now=NOW)

        # the still-pending one is skipped (has_open True); the dismissed one
        # IS attempted (has_open False) but add() rejects the duplicate
        # signature, so it does not count as new.
        assert count == 0
        assert len(second_phraser.calls) == 1
        assert second_phraser.calls[0].signature == dismissed_sig
