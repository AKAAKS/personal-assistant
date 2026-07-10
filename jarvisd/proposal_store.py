"""Habit proposals: mined-habit suggestions queued for the user to accept or dismiss.

A proposal's `signature` is a stable dedupe key for the mined pattern (see the
habit miner). The `signature` column is UNIQUE, so a given habit is proposed at
most once, ever — whatever its eventual outcome. `already_proposed` reports
whether a signature has been seen in ANY status (pending, accepted, or
dismissed); the scan pipeline uses it to skip a candidate BEFORE spending a
phrasing call. A dismissed suggestion therefore stays dismissed: JARVIS does
not re-nag (nor re-spend an API call) on a habit the user already declined.
"""

import json
import sqlite3
from datetime import datetime


class ProposalStore:
    """Stores habit proposals and serves add/list/get/accept/dismiss/already_proposed operations."""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def add(
        self,
        signature: str,
        kind: str,
        payload: dict,
        proposal_text: str,
        now: str | None = None,
    ) -> int | None:
        """Queue a new habit proposal.

        Args:
            signature: Stable dedupe key for the mined pattern.
            kind: 'rule' or 'profile' — what accepting it creates.
            payload: JSON-serializable kwargs for the eventual write
                (rules_store.add(...) kwargs for 'rule'; {"fact": ...} for 'profile').
            proposal_text: Natural-language suggestion shown to the user.
            now: Optional ISO timestamp override (for tests); defaults to now().

        Returns:
            int | None: new proposal id, or None if a proposal with this
                signature already exists (duplicate signature — caller skips).
        """
        created_ts = now if now is not None else datetime.now().isoformat(timespec="seconds")
        try:
            cur = self._conn.execute(
                "INSERT INTO habit_proposals "
                "(created_ts, signature, kind, payload, proposal_text, status) "
                "VALUES (?, ?, ?, ?, ?, 'pending')",
                (created_ts, signature, kind, json.dumps(payload), proposal_text),
            )
        except sqlite3.IntegrityError:
            return None
        self._conn.commit()
        return cur.lastrowid

    def list_pending(self) -> list[sqlite3.Row]:
        """List all pending proposals, ordered by id."""
        return self._conn.execute(
            "SELECT * FROM habit_proposals WHERE status = 'pending' ORDER BY id"
        ).fetchall()

    def get(self, pid: int) -> sqlite3.Row | None:
        """Fetch a proposal by id, or None if not found."""
        return self._conn.execute(
            "SELECT * FROM habit_proposals WHERE id = ?", (pid,)
        ).fetchone()

    def mark_accepted(self, pid: int) -> bool:
        """Transition a pending proposal to accepted.

        Returns:
            bool: True if the transition happened, False if the proposal
                doesn't exist or wasn't pending.
        """
        cur = self._conn.execute(
            "UPDATE habit_proposals SET status = 'accepted' WHERE id = ? AND status = 'pending'",
            (pid,),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def mark_dismissed(self, pid: int) -> bool:
        """Transition a pending proposal to dismissed.

        Returns:
            bool: True if the transition happened, False if the proposal
                doesn't exist or wasn't pending.
        """
        cur = self._conn.execute(
            "UPDATE habit_proposals SET status = 'dismissed' WHERE id = ? AND status = 'pending'",
            (pid,),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def already_proposed(self, signature: str) -> bool:
        """Whether a proposal with this signature exists in ANY status.

        The signature column is UNIQUE, so a habit is proposed at most once
        ever. The scan pipeline calls this to skip a candidate before spending
        a phrasing call — pending (awaiting the user), accepted (already
        applied), and dismissed (the user declined) all suppress re-proposal.
        """
        row = self._conn.execute(
            "SELECT 1 FROM habit_proposals WHERE signature = ? LIMIT 1",
            (signature,),
        ).fetchone()
        return row is not None
