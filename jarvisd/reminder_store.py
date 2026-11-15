"""Reminders: durable, restart-safe due-date tracking with catch-up recurrence."""

import calendar
import logging
import sqlite3
from datetime import datetime, timedelta

log = logging.getLogger(__name__)

_VALID_RECURRENCES = {"none", "daily", "weekdays", "weekends", "weekly", "monthly"}
_STEP = {"daily": timedelta(days=1), "weekly": timedelta(days=7)}
_DAY = timedelta(days=1)
# datetime.weekday(): Monday == 0 ... Sunday == 6
_DAY_FILTER = {
    "weekdays": lambda d: d.weekday() < 5,
    "weekends": lambda d: d.weekday() >= 5,
}

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


def _add_months(due: datetime, months: int, anchor_day: int) -> datetime:
    """`due` moved forward `months` calendar months, on `anchor_day` clamped to
    the target month's length. Wall-clock time-of-day is kept."""
    index = due.year * 12 + (due.month - 1) + months
    year, month = divmod(index, 12)
    month += 1
    day = min(anchor_day, calendar.monthrange(year, month)[1])
    return due.replace(year=year, month=month, day=day)


def _next_occurrence(
    due: datetime, recurrence: str, now: datetime, anchor_day: int | None = None
) -> datetime:
    """The first occurrence of a recurring reminder strictly after `now`.

    All values are local naive, so adding days/months preserves the wall-clock
    time-of-day across DST changes (no elapsed-duration arithmetic on aware
    datetimes). If `due` is already in the future and valid for the recurrence
    it is returned unchanged for daily/weekly (the historical catch-up loop's
    behaviour); a due on the wrong side of the week for weekdays/weekends is
    moved to the next valid day.

    monthly: the same day-of-month, clamped to the last day of a shorter month
    (31 Jan -> 28 Feb). Every candidate is computed from the original `due` and
    `anchor_day` (default due.day) rather than cumulatively, so clamping never
    ratchets the day downward when the caller supplies the anchor.

    Raises ValueError for 'none' or an unknown recurrence.
    """
    if recurrence in _STEP:
        step = _STEP[recurrence]
        while due <= now:
            due += step
        return due
    if recurrence in _DAY_FILTER:
        ok = _DAY_FILTER[recurrence]
        # Strictly after the original due, so a reminder created on a valid day
        # at `now` does not fire again the same day.
        while due <= now or not ok(due):
            due += _DAY
        return due
    if recurrence == "monthly":
        anchor = due.day if anchor_day is None else anchor_day
        months = 0
        candidate = due
        while candidate <= now:
            months += 1
            candidate = _add_months(due, months, anchor)
        return candidate
    raise ValueError(f"no next occurrence for recurrence {recurrence!r}")


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
            if recurrence not in _VALID_RECURRENCES:
                # A hand-edited/legacy value. Not 'none' (that would silently
                # complete it) and must not raise out of the reminder tick.
                log.warning(
                    "reminder %s has an unknown recurrence %r; marking it invalid",
                    reminder_id, recurrence,
                )
                self._retire_invalid(reminder_id)
                return
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
            due_ts = _next_occurrence(due_ts, recurrence, now)
            self._conn.execute(
                "UPDATE reminders SET due_ts = ? WHERE id = ? AND status = 'active'",
                (due_ts.isoformat(timespec="seconds"), reminder_id),
            )
        self._conn.commit()
