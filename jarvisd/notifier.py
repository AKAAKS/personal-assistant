"""Notifier: the single proactive-output path (speak now / phone push / queue), quiet-hours aware."""

import logging
import sqlite3
from datetime import datetime, time

log = logging.getLogger(__name__)


def _parse_quiet_hours(quiet_hours: str | None) -> tuple[time, time] | None:
    """Parse "HH:MM-HH:MM" into (start, end) times, or None if quiet hours are disabled.

    Returns None for empty/blank/None input (never quiet) and for malformed strings
    (logs a warning once, treated as disabled so a typo'd env var can't kill the daemon).
    """
    if quiet_hours is None or not quiet_hours.strip():
        return None
    try:
        start_str, end_str = quiet_hours.split("-")
        start = datetime.strptime(start_str.strip(), "%H:%M").time()
        end = datetime.strptime(end_str.strip(), "%H:%M").time()
        return start, end
    except ValueError:
        log.warning("Invalid quiet_hours format %r; disabling quiet hours", quiet_hours)
        return None


class Notifier:
    """Routes proactive output: quiet hours -> queue; high priority -> client push -> HA push -> queue; low -> queue."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        ha_client=None,
        quiet_hours: str | None = "23:00-08:00",
        now=datetime.now,
    ):
        self._conn = conn
        self.ha_client = ha_client
        self.now = now
        self._quiet_window = _parse_quiet_hours(quiet_hours)
        # Set by the server: callable(text) -> int (number of connected clients reached).
        self.push_to_clients = None

    def _is_quiet(self, now: datetime) -> bool:
        if self._quiet_window is None:
            return False
        start, end = self._quiet_window
        t = now.time()
        if start < end:
            return start <= t < end
        if start > end:
            return t >= start or t < end
        return False  # zero-length window: never quiet

    def _enqueue(self, text: str) -> None:
        try:
            ts = self.now().isoformat(timespec="seconds")
            self._conn.execute(
                "INSERT INTO notice_queue (ts, text, delivered) VALUES (?, ?, 0)",
                (ts, text),
            )
            self._conn.commit()
        except Exception as e:
            log.error("Failed to enqueue notice: %s", e)

    def notify(self, text: str, priority: str = "low") -> None:
        """Deliver or queue a proactive message. Never raises."""
        try:
            now = self.now()
            if self._is_quiet(now):
                self._enqueue(text)
                return

            if priority == "high":
                reached = 0
                if self.push_to_clients is not None:
                    try:
                        reached = self.push_to_clients(text) or 0
                    except Exception as e:
                        log.warning("push_to_clients failed: %s", e)
                        reached = 0

                if reached > 0:
                    return

                if self.ha_client is not None:
                    try:
                        self.ha_client.send_notification(text, title="JARVIS")
                        return
                    except Exception as e:
                        log.warning("ha_client.send_notification failed: %s", e)

                self._enqueue(text)
                return

            # priority == "low" (or any unrecognized value): always queue
            self._enqueue(text)
        except Exception as e:
            log.error("notify() failed unexpectedly: %s", e)

    def drain(self) -> list[str]:
        """Return undelivered notices oldest-first, marking them delivered. Never raises."""
        try:
            rows = self._conn.execute(
                "SELECT id, text FROM notice_queue WHERE delivered = 0 ORDER BY id"
            ).fetchall()
            if not rows:
                return []
            ids = [r["id"] for r in rows]
            placeholders = ",".join("?" for _ in ids)
            self._conn.execute(
                f"UPDATE notice_queue SET delivered = 1 WHERE id IN ({placeholders})",
                ids,
            )
            self._conn.commit()
            return [r["text"] for r in rows]
        except Exception as e:
            log.error("drain() failed unexpectedly: %s", e)
            return []
