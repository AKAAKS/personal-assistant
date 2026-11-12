"""Reminders: durable, restart-safe due-date tracking with catch-up recurrence."""

import logging
import sqlite3
from datetime import datetime, timedelta

log = logging.getLogger(__name__)

_VALID_RECURRENCES = {"none", "daily", "weekly"}
_STEP = {"daily": timedelta(days=1), "weekly": timedelta(days=7)}

# astimezone() on Windows raises OSError [Errno 22] for an aware value outside
# the platform's supported range (pre-1970, or near datetime.max), so parsing a
# due_ts is not ValueError-only. Every call site must tolerate the same set or
# one unusable row aborts the reminder scan -- the exact failure normalization
# was added to prevent.
BAD_DUE_TS = (ValueError, OSError, OverflowError)


def _to_local_naive(due_ts: str) -> datetime:
    """Parse an ISO datetime into LOCAL NAIVE time.

    Every stored due_ts must be naive and in one format. The model is told the
    local time with %Z, so it readily sends an offset-aware due ("...-04:00",
    "...Z"); stored as written, that row made due() -- which compares against a
    naive datetime.now() inside a list comprehension -- raise TypeError and
    return nothing, so ONE such row silently stopped every reminder and timer
    from firing. Normalizing at write time also keeps the SQL `ORDER BY due_ts`
    string sort chronological.
    """
    dt = datetime.fromisoformat(due_ts)
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt


def normalize_due_ts(due_ts: str) -> str:
    """The canonical stored form of `due_ts`: local naive, seconds precision.

    Public so a caller can echo back the time that will actually fire rather
    than the string it sent. Idempotent, so passing the result to add() again
    is safe. Raises one of BAD_DUE_TS if the value is unusable.
    """
    return _to_local_naive(due_ts).isoformat(timespec="seconds")


class ReminderStore:
    """Stores reminders and serves add/list/cancel/due/mark_fired operations."""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def add(
        self,
        text: str,
        due_ts: str,
        recurrence: str = "none",
        condition: str | None = None,
        kind: str = "reminder",
    ) -> int:
        if recurrence not in _VALID_RECURRENCES:
            raise ValueError(
                f"invalid recurrence {recurrence!r}; must be one of {sorted(_VALID_RECURRENCES)}"
            )
        try:
            due_ts = normalize_due_ts(due_ts)
        except BAD_DUE_TS as exc:
            # Not ValueError-only: see BAD_DUE_TS. Re-raised as ValueError so
            # callers keep one friendly error to catch.
            raise ValueError(f"due_ts is not a valid ISO datetime: {due_ts!r}") from exc

        created_ts = datetime.now().isoformat(timespec="seconds")
        cur = self._conn.execute(
            "INSERT INTO reminders (text, due_ts, recurrence, status, created_ts, condition, kind) "
            "VALUES (?, ?, ?, 'active', ?, ?, ?)",
            (text, due_ts, recurrence, created_ts, condition, kind),
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
        out = []
        for r in rows:
            try:
                if _to_local_naive(r["due_ts"]) <= now:
                    out.append(r)
            except BAD_DUE_TS:
                # A row add() never normalized (hand-edited, or written by an
                # older build). Retire it rather than let one bad value abort
                # the scan and keep every other reminder from firing. Retiring
                # rather than skipping matters because this scan runs every
                # REMINDER_TICK_SEC: a skipped row would warn four times a
                # minute forever, and it can never fire, so leaving it 'active'
                # would also show the user a reminder that will never arrive.
                log.warning(
                    "reminder %s has an unusable due_ts %r; marking it invalid",
                    r["id"], r["due_ts"],
                )
                self._retire_invalid(r["id"])
        return out

    def _retire_invalid(self, reminder_id: int) -> None:
        self._conn.execute(
            "UPDATE reminders SET status = 'invalid' WHERE id = ? AND status = 'active'",
            (reminder_id,),
        )
        self._conn.commit()

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
            try:
                due_ts = _to_local_naive(row["due_ts"])
            except BAD_DUE_TS:
                # Public API, and called from the reminder tick via
                # asyncio.to_thread -- raising here would kill the tick. due()
                # normally filters these out first, so this is belt-and-braces.
                log.warning(
                    "reminder %s has an unusable due_ts %r; marking it invalid",
                    reminder_id, row["due_ts"],
                )
                self._retire_invalid(reminder_id)
                return
            while due_ts <= now:
                due_ts += step
            self._conn.execute(
                "UPDATE reminders SET due_ts = ? WHERE id = ? AND status = 'active'",
                (due_ts.isoformat(timespec="seconds"), reminder_id),
            )
        self._conn.commit()
