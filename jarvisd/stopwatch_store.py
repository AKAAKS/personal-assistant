"""Stopwatches: count-up state, separate from countdown timers.

Status flow: running <-> paused, running/paused -> stopped, unparseable row -> invalid.

`accumulated_sec` holds the seconds banked from earlier running spans. While
`running`, `started_ts` is when the current span began, so elapsed is
`accumulated_sec + (now - started_ts)`. Pause banks the span; resume restarts
`started_ts`. While paused/stopped `started_ts` is stale and ignored. Every
stored timestamp is local naive, seconds precision (reminder_store.normalize_due_ts).
"""

import logging
import sqlite3
from datetime import datetime

from jarvisd.reminder_store import BAD_DUE_TS, normalize_due_ts
from jarvisd.timer_store import parse_ts, to_naive

log = logging.getLogger(__name__)

_ACTIVE = ("running", "paused")


def _iso(dt: datetime) -> str:
    return normalize_due_ts(dt.isoformat(timespec="seconds"))


def elapsed_sec(row, now: datetime) -> int:
    """Total running seconds for `row` as of `now`; the running span is clamped at 0.

    Raises one of BAD_DUE_TS on an unusable started_ts, and OSError/OverflowError
    for an absurd tz-aware `now`. Callers that retire rows must not treat the
    latter as a bad row (see StopwatchStore._elapsed_or_retire).
    """
    banked = max(0, int(row["accumulated_sec"] or 0))
    if row["status"] != "running":
        return banked
    return banked + max(0, int((to_naive(now) - parse_ts(row["started_ts"])).total_seconds()))


class StopwatchStore:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def get(self, sw_id: int) -> sqlite3.Row | None:
        return self._conn.execute("SELECT * FROM stopwatches WHERE id = ?", (sw_id,)).fetchone()

    def _retire(self, sw_id: int) -> None:
        self._conn.execute(
            "UPDATE stopwatches SET status = 'invalid' WHERE id = ? AND status IN (?, ?)",
            (sw_id, *_ACTIVE))
        self._conn.commit()

    def _elapsed_or_retire(self, row: sqlite3.Row, now: datetime) -> int | None:
        """elapsed_sec, but retire a row whose started_ts is unusable and return None.

        Only the started_ts parse is guarded. `now` is converted first, outside
        the guard, so a bad `now` raises loudly instead of retiring live rows.
        """
        now = to_naive(now)
        if row["status"] == "running":
            try:
                parse_ts(row["started_ts"])
            except BAD_DUE_TS:
                log.warning("stopwatch %s has an unusable started_ts %r; marking it invalid",
                            row["id"], row["started_ts"])
                self._retire(row["id"])
                return None
        return elapsed_sec(row, now)

    def _update(self, sql: str, params: tuple) -> bool:
        cur = self._conn.execute(sql, params)
        self._conn.commit()
        return cur.rowcount > 0

    def start(self, label: str | None = "", now: datetime | None = None) -> int:
        now = now or datetime.now()
        cur = self._conn.execute(
            "INSERT INTO stopwatches (label, started_ts, accumulated_sec, status) "
            "VALUES (?, ?, 0, 'running')", (label or "", _iso(now)))
        self._conn.commit()
        return cur.lastrowid

    def active(self, now: datetime) -> list[sqlite3.Row]:
        """Running and paused stopwatches, oldest first. Unusable rows are retired."""
        rows = self._conn.execute(
            "SELECT * FROM stopwatches WHERE status IN (?, ?) ORDER BY id", _ACTIVE).fetchall()
        return [r for r in rows if self._elapsed_or_retire(r, now) is not None]

    def pause(self, sw_id: int, now: datetime) -> bool:
        row = self.get(sw_id)
        if row is None or row["status"] != "running":
            return False
        total = self._elapsed_or_retire(row, now)
        if total is None:
            return False
        return self._update(
            "UPDATE stopwatches SET status = 'paused', accumulated_sec = ? "
            "WHERE id = ? AND status = 'running'", (total, sw_id))

    def resume(self, sw_id: int, now: datetime) -> bool:
        row = self.get(sw_id)
        if row is None or row["status"] != "paused":
            return False
        return self._update(
            "UPDATE stopwatches SET status = 'running', started_ts = ? "
            "WHERE id = ? AND status = 'paused'", (_iso(now), sw_id))

    def stop(self, sw_id: int, now: datetime) -> int | None:
        """Retire the stopwatch and return its total seconds, or None if it is
        not running/paused (unknown id, already stopped, or unusable row)."""
        row = self.get(sw_id)
        if row is None or row["status"] not in _ACTIVE:
            return None
        total = self._elapsed_or_retire(row, now)
        if total is None:
            return None
        ok = self._update(
            "UPDATE stopwatches SET status = 'stopped', accumulated_sec = ? "
            "WHERE id = ? AND status IN (?, ?)", (total, sw_id, *_ACTIVE))
        return total if ok else None
