"""Inject the rows add() can no longer create, to prove due() survives them.

Usage:  python -I poison_reminders.py <db_path> [--show]

Writes four rows straight past add(): an aware offset, a Z suffix, a pre-1970
aware value (OSError on Windows), and outright junk -- plus one good row that
must still fire. --show prints the table instead of writing.
"""
import sqlite3
import sys
from datetime import datetime, timedelta

BAD = [
    ("legacy-offset", "2026-01-01T08:00:00-05:00"),   # normalized on read -> fires
    ("legacy-zulu",   "2026-01-01T12:00:00Z"),        # normalized on read -> fires
    ("ancient-aware", "1950-06-01T12:00:00Z"),        # OSError -> retired 'invalid'
    ("pure-junk",     "tomorrow morning"),            # ValueError -> retired 'invalid'
]


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    db = sys.argv[1]
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row

    if "--backdate" in sys.argv:
        # Test D: push every recurring row three days into the past, so the
        # catch-up loop in mark_fired has something to catch up on. Here
        # rather than in a sqlite3 shell command because the sqlite3 CLI is
        # not installed on Windows by default.
        cur = conn.execute(
            "UPDATE reminders SET due_ts = ? WHERE recurrence != 'none' AND status = 'active'",
            ((datetime.now() - timedelta(days=3)).replace(
                hour=9, minute=0, second=0, microsecond=0).isoformat(timespec="seconds"),),
        )
        conn.commit()
        print(f"Backdated {cur.rowcount} recurring reminder(s) to three days ago.")
        return

    if "--show" in sys.argv:
        for r in conn.execute(
            "SELECT id, text, due_ts, recurrence, status FROM reminders ORDER BY id"
        ):
            print(f"  #{r['id']:<3} {r['status']:<8} {r['due_ts']:<28} {r['text']}")
        return

    # One good row due ~20s out: the canary. If this fires, the scan survived.
    canary = (datetime.now() + timedelta(seconds=20)).isoformat(timespec="seconds")
    rows = BAD + [("CANARY say canary fired", canary)]
    for text, due in rows:
        conn.execute(
            "INSERT INTO reminders (text, due_ts, recurrence, status, created_ts) "
            "VALUES (?, ?, 'none', 'active', ?)",
            (text, due, datetime.now().isoformat(timespec="seconds")),
        )
    conn.commit()
    print(f"Injected {len(rows)} rows into {db} (canary due {canary}).")


if __name__ == "__main__":
    main()
