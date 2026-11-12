"""What the voice client is doing right now, published for the tray to read.

The tray and the voice client are separate processes -- the tray spawns and
supervises the client -- so "I heard you and I'm thinking" has to cross a
process boundary. One small file is the whole mechanism: the client writes a
state on each transition, the tray polls it a few times a second.

Why a file and not a socket: the tray already polls on a timer, the payload is
one short record, and a file has no connection to lose when the tray restarts
the client out from under it. The OS keeps it in page cache, so polling it is
cheaper than the TCP connect the watchdog already does every 15 seconds.

Two properties make it trustworthy rather than merely simple:

  - Writes are ATOMIC (temp file + os.replace), so a poll can never catch a
    half-written record and render a state that was never true.
  - Every record carries its write time, so a client that dies mid-THINKING
    EXPIRES instead of freezing the icon on a lie. A stale read is
    indistinguishable from no read at all, which is what the tray wants: fall
    back to the supervisory state it can verify for itself.

Nothing here raises. The indicator is a nicety; the assistant is the product, so
a failed status write must never take a turn down with it.
"""

import json
import logging
import os
import tempfile
import time

log = logging.getLogger(__name__)

# --- states ----------------------------------------------------------------
# ARMED is the resting state: wake word loaded, nothing happening. It renders
# identically to the tray's own LISTENING, and exists so the client can say
# "I am back to normal" after a turn ends.
ARMED = "armed"
HEARING = "hearing"      # wake word fired; recording the user right now
THINKING = "thinking"    # audio sent; waiting for the daemon's reply
SPEAKING = "speaking"    # playing the reply
MISSED = "missed"        # the turn produced nothing -- see the reasons below

# Why a turn produced nothing. These drive blink count, not colour.
NO_WORDS = "no_words"      # audio arrived but transcribed empty
UNVERIFIED = "unverified"  # rejected as not the enrolled speaker (usually TTS echo)

PATH = os.path.join("data", "voice_activity.json")

# A record older than this is treated as absent. It has to outlast the longest
# believable THINKING (a multi-tool turn runs tens of seconds) while still
# expiring quickly enough that a crashed client stops claiming to be busy.
TTL_SEC = 90.0


def write(state: str, reason: str | None = None, *, path: str = PATH,
          now=time.time, make_parents: bool = False) -> None:
    """Publish the current state. Never raises."""
    try:
        if make_parents:
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
        record = {"state": state, "reason": reason, "ts": now()}
        directory = os.path.dirname(path) or "."
        # Atomic replace: write a sibling temp file, then rename over the target.
        # A reader either sees the whole old record or the whole new one.
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".activity-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(record, f)
            os.replace(tmp, path)
        except Exception:
            # Leave nothing behind for the tray to trip over.
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except Exception as e:
        log.debug("activity write failed (%s): %s", state, e)


def read(*, path: str = PATH, now=time.time, ttl: float = TTL_SEC):
    """The current record, or None if absent, stale or unreadable.

    Collapsing every failure to None is deliberate: the caller's fallback for
    "no information" is already correct (show the state it can verify itself),
    so there is nothing a caller could usefully do with the difference.
    """
    try:
        with open(path, encoding="utf-8") as f:
            record = json.load(f)
        if not isinstance(record, dict):
            return None
        state = record.get("state")
        if not state:
            return None
        ts = record.get("ts")
        if not isinstance(ts, (int, float)):
            return None
        if now() - ts > ttl:
            return None  # stale: the writer is gone or wedged
        return {"state": state, "reason": record.get("reason"), "ts": ts}
    except FileNotFoundError:
        return None
    except Exception as e:
        log.debug("activity read failed: %s", e)
        return None
