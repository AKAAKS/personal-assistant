"""Reminders: durable, restart-safe due-date tracking with catch-up recurrence."""

import sqlite3
from datetime import datetime, timedelta

_VALID_RECURRENCES = {"none", "daily", "weekly"}
_STEP = {"daily": timedelta(days=1), "weekly": timedelta(days=7)}


class ReminderStore:
    """Stores reminders and serves add/list/cancel/due/mark_fired operations."""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def add(self, text: str, due_ts: str, recurrence: str = "none") -> int:
        if recurrence not in _VALID_RECURRENCES:
            raise ValueError(
                f"invalid recurrence {recurrence!r}; must be one of {sorted(_VALID_RECURRENCES)}"
            )
        try:
            datetime.fromisoformat(due_ts)
        except ValueError as exc:
            raise ValueError(f"due_ts is not a valid ISO datetime: {due_ts!r}") from exc

        created_ts = datetime.now().isoformat(timespec="seconds")
        cur = self._conn.execute(
            "INSERT INTO reminders (text, due_ts, recurrence, status, created_ts) "
            "VALUES (?, ?, ?, 'active', ?)",
            (text, due_ts, recurrence, created_ts),
        )
        self._conn.commit()
        return cur.lastrowid

    def list_active(self) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM reminders WHERE status = 'active' ORDER BY due_ts"
        ).fetchall()

    def cancel(self, reminder_id: int) -> bool:
        cur = self._conn.execute(
            "UPDATE reminders SET status = 'cancelled' WHERE id = ? AND status = 'active'",
            (reminder_id,),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def due(self, now: datetime) -> list[sqlite3.Row]:
        rows = self._conn.execute(
            "SELECT * FROM reminders WHERE status = 'active' ORDER BY due_ts"
        ).fetchall()
        return [r for r in rows if datetime.fromisoformat(r["due_ts"]) <= now]

    def mark_fired(self, reminder_id: int, now: datetime) -> None:
        row = self._conn.execute(
            "SELECT recurrence, due_ts, status FROM reminders WHERE id = ?", (reminder_id,)
        ).fetchone()
        if row is None or row["status"] != "active":
            return

        recurrence = row["recurrence"]
        if recurrence == "none":
            self._conn.execute(
                "UPDATE reminders SET status = 'done' WHERE id = ? AND status = 'active'",
                (reminder_id,),
            )
        else:
            step = _STEP[recurrence]
            due_ts = datetime.fromisoformat(row["due_ts"])
            while due_ts <= now:
                due_ts += step
            self._conn.execute(
                "UPDATE reminders SET due_ts = ? WHERE id = ? AND status = 'active'",
                (due_ts.isoformat(timespec="seconds"), reminder_id),
            )
        self._conn.commit()
