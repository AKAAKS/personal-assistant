"""Tests for the pure-Python habit miner — mines audit.jsonl for time-of-day habits."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from jarvisd.habit_miner import Candidate, mine_candidates

NOW = datetime(2026, 7, 10, 12, 0, 0, tzinfo=timezone.utc)


def _local_hour(dt_utc: datetime) -> int:
    """Mirror the miner's UTC->local conversion so tests are host-timezone independent."""
    return dt_utc.astimezone().hour


def _entry(ts: datetime, tool="control_light", input=None, verdict="allowed", reason="ok"):
    return {
        "ts": ts.isoformat(),
        "tool": tool,
        "input": input if input is not None else {"entity_id": "light.bedroom", "action": "turn_on"},
        "verdict": verdict,
        "reason": reason,
    }


def _write_audit(path, entries):
    with open(path, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


class TestMineCandidatesHabit:
    def test_four_day_evening_habit_yields_one_candidate(self, tmp_path):
        audit_path = tmp_path / "audit.jsonl"
        entries = [
            _entry(datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)),
            _entry(datetime(2026, 7, 2, 12, 0, 0, tzinfo=timezone.utc)),
            _entry(datetime(2026, 7, 3, 12, 0, 0, tzinfo=timezone.utc)),
            _entry(datetime(2026, 7, 4, 12, 0, 0, tzinfo=timezone.utc)),
        ]
        _write_audit(audit_path, entries)

        candidates = mine_candidates(str(audit_path), now=NOW)

        assert len(candidates) == 1
        c = candidates[0]
        assert isinstance(c, Candidate)
        assert c.kind == "profile"
        expected_hour = _local_hour(datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc))
        assert c.evidence["hour"] == expected_hour
        assert c.evidence["tool"] == "control_light"
        assert c.evidence["count"] == 4
        assert c.evidence["days"] == 4
        assert "fact" in c.payload
        assert c.payload["fact"].startswith("Usually")
        assert isinstance(c.signature, str) and len(c.signature) > 0

    def test_signature_is_stable_across_runs(self, tmp_path):
        audit_path = tmp_path / "audit.jsonl"
        entries = [
            _entry(datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)),
            _entry(datetime(2026, 7, 2, 12, 0, 0, tzinfo=timezone.utc)),
            _entry(datetime(2026, 7, 3, 12, 0, 0, tzinfo=timezone.utc)),
            _entry(datetime(2026, 7, 4, 12, 0, 0, tzinfo=timezone.utc)),
        ]
        _write_audit(audit_path, entries)

        run1 = mine_candidates(str(audit_path), now=NOW)
        run2 = mine_candidates(str(audit_path), now=NOW)

        assert run1 == run2
        assert len(run1) == 1
        assert run1[0].signature == run2[0].signature

    def test_below_threshold_two_occurrences_yields_no_candidate(self, tmp_path):
        audit_path = tmp_path / "audit.jsonl"
        entries = [
            _entry(datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)),
            _entry(datetime(2026, 7, 2, 12, 0, 0, tzinfo=timezone.utc)),
        ]
        _write_audit(audit_path, entries)

        candidates = mine_candidates(str(audit_path), now=NOW)

        assert candidates == []

    def test_three_hits_same_calendar_day_yields_no_candidate(self, tmp_path):
        audit_path = tmp_path / "audit.jsonl"
        entries = [
            _entry(datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)),
            _entry(datetime(2026, 7, 1, 12, 7, 0, tzinfo=timezone.utc)),
            _entry(datetime(2026, 7, 1, 12, 13, 0, tzinfo=timezone.utc)),
        ]
        _write_audit(audit_path, entries)

        candidates = mine_candidates(str(audit_path), now=NOW)

        assert candidates == []

    def test_malformed_line_is_skipped_without_crashing(self, tmp_path):
        audit_path = tmp_path / "audit.jsonl"
        good_entries = [
            _entry(datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)),
            _entry(datetime(2026, 7, 2, 12, 0, 0, tzinfo=timezone.utc)),
            _entry(datetime(2026, 7, 3, 12, 0, 0, tzinfo=timezone.utc)),
            _entry(datetime(2026, 7, 4, 12, 0, 0, tzinfo=timezone.utc)),
        ]
        with open(audit_path, "w", encoding="utf-8") as f:
            f.write("{not valid json,,,\n")
            for e in good_entries:
                f.write(json.dumps(e) + "\n")
            f.write("also not json\n")

        candidates = mine_candidates(str(audit_path), now=NOW)

        assert len(candidates) == 1
        assert candidates[0].evidence["count"] == 4

    def test_blocked_verdict_entries_are_ignored(self, tmp_path):
        audit_path = tmp_path / "audit.jsonl"
        entries = [
            _entry(datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc), verdict="blocked"),
            _entry(datetime(2026, 7, 2, 12, 0, 0, tzinfo=timezone.utc), verdict="blocked"),
            _entry(datetime(2026, 7, 3, 12, 0, 0, tzinfo=timezone.utc), verdict="blocked"),
            _entry(datetime(2026, 7, 4, 12, 0, 0, tzinfo=timezone.utc), verdict="blocked"),
        ]
        _write_audit(audit_path, entries)

        candidates = mine_candidates(str(audit_path), now=NOW)

        assert candidates == []

    def test_missing_file_returns_empty_list(self, tmp_path):
        missing_path = tmp_path / "nope" / "audit.jsonl"

        candidates = mine_candidates(str(missing_path), now=NOW)

        assert candidates == []

    def test_volatile_input_field_does_not_split_group(self, tmp_path):
        audit_path = tmp_path / "audit.jsonl"
        entries = [
            _entry(
                datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc),
                input={"entity_id": "light.bedroom", "action": "turn_on", "brightness": 100},
            ),
            _entry(
                datetime(2026, 7, 2, 12, 0, 0, tzinfo=timezone.utc),
                input={"entity_id": "light.bedroom", "action": "turn_on", "brightness": 150},
            ),
            _entry(
                datetime(2026, 7, 3, 12, 0, 0, tzinfo=timezone.utc),
                input={"entity_id": "light.bedroom", "action": "turn_on", "color_name": "warm_white"},
            ),
        ]
        _write_audit(audit_path, entries)

        candidates = mine_candidates(str(audit_path), now=NOW)

        assert len(candidates) == 1
        assert candidates[0].evidence["count"] == 3
        assert candidates[0].evidence["days"] == 3

    def test_unmapped_tool_is_not_mined(self, tmp_path):
        audit_path = tmp_path / "audit.jsonl"
        entries = [
            _entry(datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc), tool="system_info", input={}),
            _entry(datetime(2026, 7, 2, 12, 0, 0, tzinfo=timezone.utc), tool="system_info", input={}),
            _entry(datetime(2026, 7, 3, 12, 0, 0, tzinfo=timezone.utc), tool="system_info", input={}),
            _entry(datetime(2026, 7, 4, 12, 0, 0, tzinfo=timezone.utc), tool="system_info", input={}),
        ]
        _write_audit(audit_path, entries)

        candidates = mine_candidates(str(audit_path), now=NOW)

        assert candidates == []

    def test_entries_outside_window_days_are_ignored(self, tmp_path):
        audit_path = tmp_path / "audit.jsonl"
        entries = [
            _entry(NOW - timedelta(days=20)),
            _entry(NOW - timedelta(days=21)),
            _entry(NOW - timedelta(days=22)),
            _entry(NOW - timedelta(days=23)),
        ]
        _write_audit(audit_path, entries)

        candidates = mine_candidates(str(audit_path), now=NOW, window_days=14)

        assert candidates == []
