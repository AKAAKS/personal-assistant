"""Countdown timers: durable state machine with pause, resume, extend, ring and snooze.

Status flow: running <-> paused, running -> ringing -> (snooze -> running | dismiss -> done),
any live state -> cancelled, unparseable row -> invalid.

`due_ts` is NULL while paused and `remaining_sec` is NULL while running; the
other column carries the clock. Every stored timestamp is local naive, seconds
precision, via reminder_store.normalize_due_ts (see its docstrings for why).
"""

import logging
import sqlite3
from datetime import datetime, timedelta

from jarvisd.reminder_store import BAD_DUE_TS, normalize_due_ts

log = logging.getLogger(__name__)

_LIVE = ("running", "paused", "ringing")


def _iso(dt: datetime) -> str:
    return normalize_due_ts(dt.isoformat(timespec="seconds"))


def parse_ts(value: str | None) -> datetime:
    """Parse a stored timestamp. Public because server, tools and stopwatch_store depend on it."""
    # NULL is handled explicitly: fromisoformat(None) raises TypeError, and
    # catching TypeError broadly would also swallow a naive/aware comparison
    # error and wrongly retire healthy rows.
    if value is None:
        raise ValueError("timestamp is NULL")
    return datetime.fromisoformat(normalize_due_ts(value))


def to_naive(now: datetime) -> datetime:
    """Local naive form of `now`, so subtraction can never raise a naive/aware TypeError.

    Public because server, tools and stopwatch_store depend on it.
    """
    if now.tzinfo is not None:
        now = now.astimezone().replace(tzinfo=None)
    return now


def remaining_for(row: sqlite3.Row, now: datetime) -> int:
    """Seconds left on `row`, clamped at 0. Raises one of BAD_DUE_TS on a bad due_ts."""
    if row["status"] == "paused":
        return max(0, int(row["remaining_sec"] or 0))
    return max(0, int((parse_ts(row["due_ts"]) - to_naive(now)).total_seconds()))


class TimerStore:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    # ---- reads -----------------------------------------------------------

    def get(self, timer_id: int) -> sqlite3.Row | None:
        return self._conn.execute("SELECT * FROM timers WHERE id = ?", (timer_id,)).fetchone()

    def _remaining_or_retire(self, row: sqlite3.Row, now: datetime) -> int | None:
        """remaining_for, but retire an unusable row and return None instead of raising."""
        try:
            return remaining_for(row, now)
        except BAD_DUE_TS:
            # Retired rather than skipped: a skipped row would warn on every
            # tick forever and could never fire.
            log.warning("timer %s has an unusable due_ts %r; marking it invalid",
                        row["id"], row["due_ts"])
            self._retire(row["id"])
            return None

    def _retire(self, timer_id: int) -> None:
        self._conn.execute(
            "UPDATE timers SET status = 'invalid' WHERE id = ? AND status IN (?, ?, ?, ?)",
            (timer_id, *_LIVE, "done"),
        )
        self._conn.commit()

    def list_live(self, now: datetime) -> list[sqlite3.Row]:
        """Running, paused and ringing timers, least time remaining first.

        `now` is needed because a paused timer has no due_ts: its frozen
        remainder must be compared against a running timer's computed one.
        """
        rows = self._conn.execute(
            "SELECT * FROM timers WHERE status IN (?, ?, ?) ORDER BY id", _LIVE
        ).fetchall()
        keyed = []
        for r in rows:
            rem = self._remaining_or_retire(r, now)
            if rem is not None:
                keyed.append((rem, r["id"], r))
        keyed.sort(key=lambda t: t[:2])
        return [t[2] for t in keyed]

    def recently_fired(self, now: datetime, within_sec: int = 1800) -> list[sqlite3.Row]:
        """Timers that rang within the window (newest first), including dismissed ones."""
        # The window is applied in SQL so the scan does not grow with every timer
        # ever set. `datetime(fired_ts) IS NULL` keeps unparseable stamps in the
        # result so the loop below still retires them, as it did before.
        now = to_naive(now)
        cutoff = now - timedelta(seconds=within_sec)
        rows = self._conn.execute(
            "SELECT * FROM timers WHERE fired_ts IS NOT NULL "
            "AND status IN ('ringing', 'done') "
            "AND (fired_ts >= ? OR datetime(fired_ts) IS NULL) "
            "ORDER BY fired_ts DESC, id DESC",
            (cutoff.isoformat(timespec="seconds"),),
        ).fetchall()
        out = []
        for r in rows:
            try:
                fired = parse_ts(r["fired_ts"])
            except BAD_DUE_TS:
                log.warning("timer %s has an unusable fired_ts %r; marking it invalid",
                            r["id"], r["fired_ts"])
                self._retire(r["id"])
                continue
            if cutoff <= fired <= now:
                out.append(r)
        return out

    def find_by_label(self, label: str) -> list[sqlite3.Row]:
        wanted = label.casefold()
        rows = self._conn.execute(
            "SELECT * FROM timers WHERE status IN (?, ?, ?) ORDER BY id", _LIVE
        ).fetchall()
        return [r for r in rows if r["label"].casefold() == wanted]

    def due(self, now: datetime) -> list[sqlite3.Row]:
        rows = self._conn.execute(
            "SELECT * FROM timers WHERE status = 'running' ORDER BY id"
        ).fetchall()
        out = []
        for r in rows:
            if self._remaining_or_retire(r, now) == 0:
                out.append(r)
        out.sort(key=lambda r: (r["due_ts"], r["id"]))
        return out

    # ---- mutations -------------------------------------------------------

    def _update(self, sql: str, params: tuple) -> bool:
        cur = self._conn.execute(sql, params)
        self._conn.commit()
        return cur.rowcount > 0

    def add(self, duration_sec: int, label: str = "", now: datetime | None = None) -> int:
        now = now or datetime.now()
        cur = self._conn.execute(
            "INSERT INTO timers (label, duration_sec, due_ts, status, created_ts) "
            "VALUES (?, ?, ?, 'running', ?)",
            (label, int(duration_sec), _iso(now + timedelta(seconds=duration_sec)), _iso(now)),
        )
        self._conn.commit()
        return cur.lastrowid

    def pause(self, timer_id: int, now: datetime) -> bool:
        row = self.get(timer_id)
        if row is None or row["status"] != "running":
            return False
        rem = self._remaining_or_retire(row, now)
        if rem is None:
            return False
        return self._update(
            "UPDATE timers SET status = 'paused', remaining_sec = ?, due_ts = NULL "
            "WHERE id = ? AND status = 'running'", (rem, timer_id))

    def resume(self, timer_id: int, now: datetime) -> bool:
        row = self.get(timer_id)
        if row is None or row["status"] != "paused":
            return False
        due = _iso(now + timedelta(seconds=row["remaining_sec"] or 0))
        return self._update(
            "UPDATE timers SET status = 'running', due_ts = ?, remaining_sec = NULL "
            "WHERE id = ? AND status = 'paused'", (due, timer_id))

    def extend(self, timer_id: int, extra_sec: int, now: datetime) -> bool:
        """Add time to a running timer (due moves out) or a paused one (remainder grows)."""
        row = self.get(timer_id)
        if row is None:
            return False
        if row["status"] == "paused":
            return self._update(
                "UPDATE timers SET remaining_sec = remaining_sec + ? "
                "WHERE id = ? AND status = 'paused'", (int(extra_sec), timer_id))
        if row["status"] != "running":
            return False
        if self._remaining_or_retire(row, now) is None:
            return False
        due = _iso(parse_ts(row["due_ts"]) + timedelta(seconds=extra_sec))
        return self._update(
            "UPDATE timers SET due_ts = ? WHERE id = ? AND status = 'running'", (due, timer_id))

    def cancel(self, timer_id: int) -> bool:
        return self._update(
            "UPDATE timers SET status = 'cancelled' WHERE id = ? AND status IN (?, ?, ?)",
            (timer_id, *_LIVE))

    def rename(self, timer_id: int, label: str) -> bool:
        """Set the label of a live (running, paused or ringing) timer."""
        return self._update(
            "UPDATE timers SET label = ? WHERE id = ? AND status IN (?, ?, ?)",
            (label, timer_id, *_LIVE))

    def mark_ringing(self, timer_id: int, now: datetime) -> None:
        """running -> ringing. Resets ring_count: it counts rings in the current
        ring cycle, so a snoozed alarm gets a fresh budget when it rings again."""
        self._update(
            "UPDATE timers SET status = 'ringing', fired_ts = ?, ring_count = 0 WHERE id = ? AND status = 'running'",
            (_iso(now), timer_id))

    def bump_ring(self, timer_id: int) -> int:
        self._update(
            "UPDATE timers SET ring_count = ring_count + 1 WHERE id = ? AND status = 'ringing'",
            (timer_id,))
        row = self.get(timer_id)
        return row["ring_count"] if row else 0

    def dismiss(self, timer_id: int) -> bool:
        return self._update(
            "UPDATE timers SET status = 'done' WHERE id = ? AND status IN (?, ?, ?)",
            (timer_id, *_LIVE))

    def dismiss_ringing(self, timer_id: int) -> bool:
        """Dismiss only if still ringing; a no-op against a snooze, pause or cancel that got in first."""
        return self._update(
            "UPDATE timers SET status = 'done' WHERE id = ? AND status = 'ringing'",
            (timer_id,))

    def snooze(self, timer_id: int, snooze_sec: int, now: datetime) -> bool:
        return self._update(
            "UPDATE timers SET status = 'running', due_ts = ?, remaining_sec = NULL "
            "WHERE id = ? AND status = 'ringing'",
            (_iso(now + timedelta(seconds=snooze_sec)), timer_id))
