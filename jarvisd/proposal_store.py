"""Habit proposals: mined-habit suggestions queued for the user to accept or dismiss.

A proposal's `signature` is a stable dedupe key for the mined pattern (see the
habit miner) so the same habit is never re-proposed while a prior proposal on
it is still pending or has been accepted. A *dismissed* proposal does NOT
block re-proposal: `has_open` only reports pending/accepted as open, so a
genuinely recurring habit the user dismissed once can resurface later if it
keeps happening.
"""

import json
import sqlite3
from datetime import datetime

_OPEN_STATUSES = ("pending", "accepted")


class ProposalStore:
    """Stores habit proposals and serves add/list/get/accept/dismiss/has_open operations."""

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

    def has_open(self, signature: str) -> bool:
        """Whether a proposal with this signature is still 'open' (pending or accepted).

        Dismissed proposals do NOT count as open, so a recurring habit the
        user dismissed once may be proposed again later.
        """
        row = self._conn.execute(
            "SELECT 1 FROM habit_proposals WHERE signature = ? AND status IN (?, ?) LIMIT 1",
            (signature, *_OPEN_STATUSES),
        ).fetchone()
        return row is not None
