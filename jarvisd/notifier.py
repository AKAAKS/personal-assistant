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


# Priorities allowed to interrupt. "urgent" additionally ignores quiet hours:
# it is for a countdown the user set themselves and is actively waiting on, where
# staying quiet defeats the entire point of the timer. Scheduled and recurring
# reminders stay "high" and keep respecting the quiet window.
INTERRUPTING = frozenset({"high", "urgent"})
IGNORES_QUIET_HOURS = frozenset({"urgent"})


class Notifier:
    """Routes proactive output.

    Delivery order for an interrupting notice: speak it through a connected
    client -> local toast+chime on this machine -> Home Assistant phone push ->
    queue for the next turn. Low priority always queues.

    The local step exists because the first and third are both routinely
    unavailable: no voice client is connected unless the user launched one, and
    the HA push needs a reachable HA. Without it a timer could come due with
    nothing on the machine the user was sitting at making a sound.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        ha_client=None,
        quiet_hours: str | None = "23:00-08:00",
        now=datetime.now,
        local_notify=None,
    ):
        self._conn = conn
        self.ha_client = ha_client
        self.now = now
        self._quiet_window = _parse_quiet_hours(quiet_hours)
        # Set by the server: callable(text) -> int (number of connected clients reached).
        self.push_to_clients = None
        # Optional callable(text) -> bool: raise a notification on THIS machine
        # (Windows toast + chime). False/None means unavailable, so routing falls
        # through to the phone push. See jarvisd.local_notify.
        self.local_notify = local_notify

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

    def notify(self, text: str, priority: str = "low", *,
               perishable: bool = False) -> None:
        """Deliver or queue a proactive message. Never raises.

        `perishable` means the notice is only true now: deliver it through a
        live route or drop it, never bank it. "Claude has been waiting on you"
        banked at midnight and read out at 8am, about a session that ended
        hours ago, is worse than silence -- and the queue is designed to do
        exactly that, since drain() appends to the next turn whenever it comes.

        It is keyword-only so no existing positional call site changes, and it
        defaults to False because reminders and timers DO want to be banked.
        """
        try:
            now = self.now()
            if self._is_quiet(now) and priority not in IGNORES_QUIET_HOURS:
                if perishable:
                    log.info("notify[%s] -> dropped (quiet hours, perishable): %s",
                             priority, text)
                    return
                log.info("notify[%s] -> queued (quiet hours): %s", priority, text)
                self._enqueue(text)
                return

            if priority in INTERRUPTING:
                reached = 0
                if self.push_to_clients is not None:
                    try:
                        reached = self.push_to_clients(text) or 0
                    except Exception as e:
                        log.warning("push_to_clients failed: %s", e)
                        reached = 0

                if reached > 0:
                    log.info("notify[%s] -> spoken to %d client(s): %s",
                             priority, reached, text)
                    return

                # Nobody was listening. Make a noise on this machine before
                # reaching for the phone: the user is most likely sitting here.
                if self.local_notify is not None:
                    try:
                        if self.local_notify(text):
                            log.info("notify[%s] -> local toast/chime: %s", priority, text)
                            return
                        log.info("notify[%s] -> local notifier declined", priority)
                    except Exception as e:
                        log.warning("local_notify failed: %s", e)

                if self.ha_client is not None:
                    try:
                        self.ha_client.send_notification(text, title="JARVIS")
                        log.info("notify[%s] -> HA phone push: %s", priority, text)
                        return
                    except Exception as e:
                        log.warning("ha_client.send_notification failed: %s", e)

                if perishable:
                    log.info("notify[%s] -> dropped (no route, perishable): %s",
                             priority, text)
                    return
                log.info("notify[%s] -> queued (no route available): %s", priority, text)
                self._enqueue(text)
                return

            # priority == "low" (or any unrecognized value): always queue
            if perishable:
                log.info("notify[%s] -> dropped (perishable, not interrupting): %s",
                         priority, text)
                return
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
