# Alarm-Grade Timers & Google Home Parity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A timer that actually wakes the user (every channel at once, ringing until dismissed), whose state JARVIS can report at any time, with the controls and recurrence a Google Home has.

**Architecture:** Countdown timers move out of the `reminders` table into a dedicated `timers` table, because pause/resume/extend/ring/snooze need state a due-date row cannot carry; scheduled reminders stay in `reminders` and only gain richer recurrence. `Notifier` gains an `alarm` mode that fans out to every channel instead of returning on the first success, and the server grows a re-ring loop that keeps a fired timer sounding until dismissed. A stopwatch is a third, trivial store.

**Tech Stack:** Python 3.12, sqlite3, asyncio, pytest. Existing `winotify` toast + `winsound` chime via `jarvisd/local_notify.py`.

**Spec:** `docs/superpowers/specs/2026-11-15-app-launch-and-alarms-design.md` (Part B)

## Global Constraints

- Every timestamp stored is **local naive, seconds precision**, produced through `jarvisd.reminder_store.normalize_due_ts`. The module docstring records why: one offset-aware row once stopped every reminder and timer from firing.
- Catch every parse of a stored timestamp with `jarvisd.reminder_store.BAD_DUE_TS` (`ValueError, OSError, OverflowError`) — `astimezone()` raises `OSError [Errno 22]` on Windows for out-of-range values.
- Nothing in the notify path may raise: `Notifier.notify` is called from inside the reminder tick, where an exception is a failed tick and an infinite retry.
- A timer is `urgent` and ignores quiet hours; a scheduled reminder stays `high` and respects them. Do not change `INTERRUPTING` or `IGNORES_QUIET_HOURS` semantics for non-alarm notices.
- Migrations are append-only: add key `11` to `MIGRATIONS` in `jarvisd/db.py` and bump `SCHEMA_VERSION` to `11`. Never edit an existing migration.
- New tools go in `security/policy.py:TOOL_PERMISSIONS`. Timer reads are `read_only`; timer mutations are `write`. **No timer tool may be `destructive`** — a confirmation prompt on "stop the timer" is the bug this plan exists to prevent.
- Spoken output: remaining time is rendered for text-to-speech ("4 minutes 30 seconds"), never as a bare ISO timestamp or `0:04:30`.
- Baseline on master at planning time: **1479 passed, 3 failed**. All 3 failures
  (`tests/test_server_habits.py` x2, `tests/test_server_proactive.py::test_serve_forever_wires_push_to_clients_and_starts_reminder_task`)
  are environmental: they bind the real port 8765, which the live daemon holds
  (`OSError [Errno 10048]`). They pass with the daemon stopped and are NOT
  regressions. Task 5 modifies the `serve_forever` wiring those tests assert on,
  so **stop the daemon before running them** and confirm all three pass there
  before claiming Task 5 done. Every other test must stay green throughout.

## Review Focus

Inputs the spec implies but no happy-path test exercises, most likely to bite first:

1. The daemon restarts while a timer is running, or while one is ringing — a timer whose due time passed during downtime must fire on the next tick, not be lost or silently marked done (Task 2 + Task 5).
2. A paused timer must never come due: its remaining time is frozen, so any tick comparing a stale `due_ts` against `now` would fire it immediately on pause (Task 2).
3. Two timers with the same or no label ("set a timer" twice) — cancel-by-label and dismiss must disambiguate rather than act on an arbitrary one (Task 6).
4. A timer dismissed from one channel while still ringing on another, and a dismiss arriving for a timer that already stopped — both must be idempotent, never raise (Task 5).
5. `monthly` recurrence on the 31st, and a `weekdays` reminder created on a Saturday — the next occurrence must be a real date, and the `while due <= now` advance loop must terminate (Task 8).

---

### Task 1: Schema for timers and stopwatches

**Files:**
- Modify: `jarvisd/db.py` — add `MIGRATIONS[11]`, bump `SCHEMA_VERSION` to `11`
- Test: `tests/test_db.py`

**Interfaces:**
- Produces: tables `timers` and `stopwatches`.

`timers` columns: `id INTEGER PRIMARY KEY`, `label TEXT NOT NULL DEFAULT ''`, `duration_sec INTEGER NOT NULL`, `due_ts TEXT`, `remaining_sec INTEGER`, `status TEXT NOT NULL DEFAULT 'running'`, `created_ts TEXT NOT NULL`, `fired_ts TEXT`, `ring_count INTEGER NOT NULL DEFAULT 0`.
`status` is one of `running | paused | ringing | done | cancelled`. `due_ts` is set when running or ringing and `NULL` when paused; `remaining_sec` is set when paused and `NULL` otherwise.

`stopwatches` columns: `id INTEGER PRIMARY KEY`, `label TEXT NOT NULL DEFAULT ''`, `started_ts TEXT NOT NULL`, `accumulated_sec INTEGER NOT NULL DEFAULT 0`, `status TEXT NOT NULL DEFAULT 'running'`.

- [ ] **Step 1: Write the failing test**

```python
def test_migration_11_creates_timers_and_stopwatches(tmp_path):
    conn = db.connect(str(tmp_path / "t.db"))
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(timers)")}
    assert cols == {"id", "label", "duration_sec", "due_ts", "remaining_sec",
                    "status", "created_ts", "fired_ts", "ring_count"}
    assert {r["name"] for r in conn.execute("PRAGMA table_info(stopwatches)")} == {
        "id", "label", "started_ts", "accumulated_sec", "status"}


def test_schema_version_is_11(tmp_path):
    conn = db.connect(str(tmp_path / "t.db"))
    assert conn.execute("SELECT MAX(version) v FROM schema_version").fetchone()["v"] == 11


def test_existing_database_upgrades_without_losing_reminders(tmp_path):
    # An existing install is at version 10 with live reminder rows.
    p = str(tmp_path / "t.db")
    conn = db.connect(p)
    conn.execute("INSERT INTO reminders (text, due_ts, created_ts) VALUES ('x','2026-01-01T00:00:00','2026-01-01T00:00:00')")
    conn.commit()
    conn.close()
    conn2 = db.connect(p)
    assert conn2.execute("SELECT COUNT(*) c FROM reminders").fetchone()["c"] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_db.py -k "migration_11 or version_is_11" -v`
Expected: FAIL — `no such table: timers`

- [ ] **Step 3: Add `MIGRATIONS[11]` in `jarvisd/db.py`**

Write the two `CREATE TABLE` statements and bump `SCHEMA_VERSION`. Add a comment, in the style of migration 10's, recording that timers left the `reminders` table because pause/ring/snooze need their own state.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_db.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add jarvisd/db.py tests/test_db.py
git commit -m "feat: schema for timers and stopwatches"
```

---

### Task 2: TimerStore

**Files:**
- Create: `jarvisd/timer_store.py`
- Test: `tests/test_timer_store.py`

**Interfaces:**
- Consumes: `normalize_due_ts`, `BAD_DUE_TS` from `jarvisd.reminder_store`.
- Produces: `class TimerStore(conn: sqlite3.Connection)` with:
  - `add(duration_sec: int, label: str = "", now: datetime | None = None) -> int`
  - `list_live(now: datetime) -> list[sqlite3.Row]` — status in `running|paused|ringing`, soonest first
  - `recently_fired(now: datetime, within_sec: int = 1800) -> list[sqlite3.Row]`
  - `get(timer_id: int) -> sqlite3.Row | None`
  - `find_by_label(label: str) -> list[sqlite3.Row]` — live timers whose label matches case-insensitively
  - `due(now: datetime) -> list[sqlite3.Row]` — `status='running'` and `due_ts <= now`
  - `pause(timer_id: int, now: datetime) -> bool`
  - `resume(timer_id: int, now: datetime) -> bool`
  - `extend(timer_id: int, extra_sec: int, now: datetime) -> bool`
  - `cancel(timer_id: int) -> bool`
  - `mark_ringing(timer_id: int, now: datetime) -> None`
  - `bump_ring(timer_id: int) -> int` — increments and returns `ring_count`
  - `dismiss(timer_id: int) -> bool` — `ringing|running|paused` -> `done`; idempotent
  - `snooze(timer_id: int, snooze_sec: int, now: datetime) -> bool` — `ringing` -> `running`, due `now + snooze_sec`
  - `remaining_for(row: sqlite3.Row, now: datetime) -> int` (module-level function, clamped at 0; named so it cannot be confused with the `remaining_sec` column)

- [ ] **Step 1: Write the failing test**

```python
from datetime import datetime, timedelta

T0 = datetime(2026, 11, 15, 10, 0, 0)


def test_add_sets_due_and_running(store):
    tid = store.add(600, "tea", now=T0)
    row = store.get(tid)
    assert row["status"] == "running"
    assert row["label"] == "tea"
    assert row["due_ts"] == "2026-11-15T10:10:00"
    assert row["remaining_sec"] is None


def test_remaining_counts_down(store):
    tid = store.add(600, now=T0)
    from jarvisd.timer_store import remaining_for
    assert remaining_for(store.get(tid), T0 + timedelta(minutes=4)) == 360


def test_remaining_never_goes_negative(store):
    tid = store.add(60, now=T0)
    from jarvisd.timer_store import remaining_for
    assert remaining_for(store.get(tid), T0 + timedelta(hours=2)) == 0


def test_pause_freezes_remaining_and_clears_due(store):
    # A paused timer must never come due; a stale due_ts would fire instantly.
    tid = store.add(600, now=T0)
    assert store.pause(tid, T0 + timedelta(minutes=2)) is True
    row = store.get(tid)
    assert row["status"] == "paused"
    assert row["remaining_sec"] == 480
    assert row["due_ts"] is None
    assert store.due(T0 + timedelta(hours=1)) == []


def test_resume_restores_the_frozen_remainder(store):
    tid = store.add(600, now=T0)
    store.pause(tid, T0 + timedelta(minutes=2))
    store.resume(tid, T0 + timedelta(hours=5))      # paused for hours
    row = store.get(tid)
    assert row["status"] == "running"
    assert row["due_ts"] == "2026-11-15T15:08:00"   # 8 min from resume
    assert row["remaining_sec"] is None


def test_pause_is_rejected_for_a_timer_that_is_not_running(store):
    tid = store.add(600, now=T0)
    store.cancel(tid)
    assert store.pause(tid, T0) is False


def test_extend_pushes_due_out_while_running(store):
    tid = store.add(600, now=T0)
    store.extend(tid, 300, T0)
    assert store.get(tid)["due_ts"] == "2026-11-15T10:15:00"


def test_extend_adds_to_the_remainder_while_paused(store):
    tid = store.add(600, now=T0)
    store.pause(tid, T0 + timedelta(minutes=2))
    store.extend(tid, 300, T0)
    assert store.get(tid)["remaining_sec"] == 780


def test_due_returns_only_elapsed_running_timers(store):
    a = store.add(60, now=T0)
    store.add(6000, now=T0)
    assert [r["id"] for r in store.due(T0 + timedelta(minutes=5))] == [a]


def test_a_timer_due_during_downtime_still_fires_after_restart(store):
    # The daemon was off when it came due; it must not be lost.
    tid = store.add(60, now=T0)
    assert [r["id"] for r in store.due(T0 + timedelta(days=1))] == [tid]


def test_mark_ringing_then_dismiss(store):
    tid = store.add(60, now=T0)
    store.mark_ringing(tid, T0 + timedelta(minutes=1))
    assert store.get(tid)["status"] == "ringing"
    assert store.get(tid)["fired_ts"] == "2026-11-15T10:01:00"
    assert store.dismiss(tid) is True
    assert store.get(tid)["status"] == "done"


def test_dismiss_is_idempotent(store):
    tid = store.add(60, now=T0)
    store.dismiss(tid)
    assert store.dismiss(tid) is False          # no longer live, but no raise


def test_snooze_puts_a_ringing_timer_back_to_running(store):
    tid = store.add(60, now=T0)
    store.mark_ringing(tid, T0 + timedelta(minutes=1))
    store.snooze(tid, 540, T0 + timedelta(minutes=1))
    row = store.get(tid)
    assert row["status"] == "running"
    assert row["due_ts"] == "2026-11-15T10:10:00"


def test_recently_fired_surfaces_a_done_timer(store):
    # "Did my timer go off?" was unanswerable: done rows were invisible.
    tid = store.add(60, "pasta", now=T0)
    store.mark_ringing(tid, T0 + timedelta(minutes=1))
    store.dismiss(tid)
    assert [r["id"] for r in store.recently_fired(T0 + timedelta(minutes=10))] == [tid]
    assert store.recently_fired(T0 + timedelta(hours=5)) == []


def test_find_by_label_is_case_insensitive_and_returns_all_matches(store):
    store.add(60, "Tea", now=T0)
    store.add(60, "tea", now=T0)
    assert len(store.find_by_label("TEA")) == 2


def test_list_live_is_soonest_first_and_excludes_finished(store):
    far = store.add(6000, now=T0)
    soon = store.add(60, now=T0)
    gone = store.add(60, now=T0)
    store.cancel(gone)
    assert [r["id"] for r in store.list_live(T0)] == [soon, far]


def test_an_unparseable_due_ts_does_not_abort_the_scan(store, conn):
    good = store.add(60, now=T0)
    conn.execute("INSERT INTO timers (duration_sec, due_ts, status, created_ts) "
                 "VALUES (60, 'not-a-date', 'running', '2026-11-15T10:00:00')")
    conn.commit()
    assert [r["id"] for r in store.due(T0 + timedelta(minutes=5))] == [good]
```

Add a `conftest`-style fixture in the test module: `conn` from `jarvisd.db.connect(tmp_path)` and `store = TimerStore(conn)`.

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_timer_store.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'jarvisd.timer_store'`

- [ ] **Step 3: Implement `TimerStore` and `remaining_sec` in `jarvisd/timer_store.py`**

`remaining_for(row, now)`: when `status == 'paused'` return `row["remaining_sec"]`; otherwise `max(0, int((parse(row["due_ts"]) - now).total_seconds()))`. Follow `ReminderStore.due`'s precedent for a bad timestamp: log a warning and set `status='invalid'` rather than let one row abort the scan. Every mutation guards on the current status in the `WHERE` clause and returns `rowcount > 0`, as `ReminderStore.cancel` does.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_timer_store.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add jarvisd/timer_store.py tests/test_timer_store.py
git commit -m "feat: TimerStore with pause, resume, extend, ring and snooze"
```

---

### Task 3: Spoken duration formatting

**Files:**
- Modify: `jarvisd/text_format.py`
- Test: `tests/test_text_format.py`

**Interfaces:**
- Produces: `speak_duration(total_sec: int) -> str`

- [ ] **Step 1: Write the failing test**

```python
import pytest
from jarvisd.text_format import speak_duration


@pytest.mark.parametrize("sec,expected", [
    (0, "no time"),
    (1, "1 second"),
    (45, "45 seconds"),
    (60, "1 minute"),
    (90, "1 minute 30 seconds"),
    (270, "4 minutes 30 seconds"),
    (600, "10 minutes"),
    (3600, "1 hour"),
    (3660, "1 hour 1 minute"),
    (7325, "2 hours 2 minutes"),
])
def test_speak_duration(sec, expected):
    assert speak_duration(sec) == expected


def test_speak_duration_drops_seconds_once_past_an_hour():
    # "2 hours 2 minutes 5 seconds" is noise in speech.
    assert speak_duration(7325) == "2 hours 2 minutes"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_text_format.py -k speak_duration -v`
Expected: FAIL with `ImportError: cannot import name 'speak_duration'`

- [ ] **Step 3: Implement `speak_duration(total_sec: int) -> str` in `jarvisd/text_format.py`**

Emit at most two units, largest first; omit a zero unit; singularise at 1. Drop the seconds component entirely when hours are present.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_text_format.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add jarvisd/text_format.py tests/test_text_format.py
git commit -m "feat: speak_duration for spoken remaining-time"
```

---

### Task 4: Notifier alarm mode fans out to every channel

**Files:**
- Modify: `jarvisd/notifier.py` — `notify` signature and the `INTERRUPTING` branch
- Test: `tests/test_notifier_alarm.py`

**Interfaces:**
- Produces: `Notifier.notify(text, priority="low", *, perishable=False, local_only=False, alarm=False) -> None`.
  With `alarm=True`: attempt the client push **and** `local_notify` **and** (unless `local_only`) the HA push, regardless of which already succeeded; enqueue only when every channel failed. Keyword-only and defaulting `False`, so no existing call site changes behaviour.

- [ ] **Step 1: Write the failing test**

```python
def test_alarm_rings_every_channel_even_when_a_client_was_reached(notifier, spy):
    # THE BUG: notify returned on the first success, so a timer that was spoken
    # never produced the chime or the toast. The user heard one 2.3s line.
    notifier.notify("Timer's up.", "urgent", alarm=True)
    assert spy.pushed == ["Timer's up."]
    assert spy.local == ["Timer's up."]
    assert spy.ha == ["Timer's up."]


def test_a_non_alarm_notice_still_stops_at_the_first_channel(notifier, spy):
    notifier.notify("Reminder: bins", "high")
    assert spy.pushed and not spy.local and not spy.ha


def test_alarm_still_lands_locally_when_no_client_is_connected(notifier, spy):
    spy.push_result = 0
    notifier.notify("Timer's up.", "urgent", alarm=True)
    assert spy.local == ["Timer's up."]


def test_alarm_is_queued_only_when_every_channel_fails(notifier, spy, conn):
    spy.push_result = 0
    spy.local_raises = spy.ha_raises = True
    notifier.notify("Timer's up.", "urgent", alarm=True)
    assert conn.execute("SELECT COUNT(*) c FROM notice_queue").fetchone()["c"] == 1


def test_one_failing_channel_does_not_stop_the_others(notifier, spy):
    spy.local_raises = True
    notifier.notify("Timer's up.", "urgent", alarm=True)
    assert spy.pushed and spy.ha


def test_an_urgent_alarm_ignores_quiet_hours(notifier_in_quiet_hours, spy):
    notifier_in_quiet_hours.notify("Timer's up.", "urgent", alarm=True)
    assert spy.local == ["Timer's up."]
```

Build the `spy` fixture as a single object exposing `pushed`/`local`/`ha` lists, a settable `push_result`, and `local_raises`/`ha_raises` flags, wired into `Notifier` as `push_to_clients`, `local_notify`, and an `ha_client` stub with `send_notification`. Follow `tests/test_notifier_local.py` for the existing fixture style.

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_notifier_alarm.py -v`
Expected: FAIL — `notify() got an unexpected keyword argument 'alarm'`

- [ ] **Step 3: Add the `alarm` fan-out to `Notifier.notify`**

In the `INTERRUPTING` branch, track `delivered = False` across the three channels. When `alarm` is false keep the existing early `return` after each success; when true, record success and continue. Each channel stays inside its own `try/except` so one failure cannot stop the rest. Update the class docstring to describe both modes.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_notifier_alarm.py tests/test_notifier.py tests/test_notifier_local.py tests/test_notifier_perishable.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add jarvisd/notifier.py tests/test_notifier_alarm.py
git commit -m "feat: alarm notices ring every channel instead of the first"
```

---

### Task 5: The server rings timers until dismissed

**Files:**
- Modify: `jarvisd/server.py` — add `timer_store` constructor arg, a `_timer_tick` task alongside `_reminder_task`, and `_fire_due_timers`
- Test: `tests/test_server_timers.py`

**Interfaces:**
- Consumes: `TimerStore` (Task 2), `speak_duration` (Task 3), `notify(..., alarm=True)` (Task 4).
- Produces: `TIMER_TICK_SEC = 5`, `RING_INTERVAL_SEC = 30`, `MAX_RINGS = 20`; `Server._fire_due_timers()` coroutine.

Behaviour: each tick, every `due()` timer is marked ringing and alarmed once; every already-`ringing` timer re-alarms when `RING_INTERVAL_SEC` has passed since its last ring, until `MAX_RINGS`, then auto-dismisses so it cannot ring forever.

- [ ] **Step 1: Write the failing test**

```python
async def test_a_due_timer_rings_as_an_alarm(server, timer_store, spy_notifier):
    timer_store.add(60, "tea", now=T0)
    await server._fire_due_timers(now=T0 + timedelta(minutes=2))
    call = spy_notifier.calls[0]
    assert call.kwargs["alarm"] is True
    assert call.kwargs.get("priority", call.args[1]) == "urgent"
    assert "tea" in call.args[0]


async def test_a_ringing_timer_keeps_ringing_until_dismissed(server, timer_store, spy_notifier):
    # THE BUG: it alerted once, 2.3 seconds, and went quiet.
    tid = timer_store.add(60, now=T0)
    t = T0 + timedelta(minutes=2)
    await server._fire_due_timers(now=t)
    await server._fire_due_timers(now=t + timedelta(seconds=30))
    await server._fire_due_timers(now=t + timedelta(seconds=60))
    assert len(spy_notifier.calls) == 3
    timer_store.dismiss(tid)
    await server._fire_due_timers(now=t + timedelta(seconds=90))
    assert len(spy_notifier.calls) == 3


async def test_it_does_not_re_ring_before_the_interval(server, timer_store, spy_notifier):
    timer_store.add(60, now=T0)
    t = T0 + timedelta(minutes=2)
    await server._fire_due_timers(now=t)
    await server._fire_due_timers(now=t + timedelta(seconds=5))
    assert len(spy_notifier.calls) == 1


async def test_ringing_stops_and_auto_dismisses_at_max_rings(server, timer_store, spy_notifier):
    tid = timer_store.add(60, now=T0)
    t = T0 + timedelta(minutes=2)
    for i in range(30):
        await server._fire_due_timers(now=t + timedelta(seconds=30 * i))
    assert len(spy_notifier.calls) <= 20
    assert timer_store.get(tid)["status"] == "done"


async def test_a_paused_timer_never_rings(server, timer_store, spy_notifier):
    tid = timer_store.add(60, now=T0)
    timer_store.pause(tid, T0)
    await server._fire_due_timers(now=T0 + timedelta(hours=1))
    assert spy_notifier.calls == []


async def test_one_bad_timer_does_not_stop_the_others(server, timer_store, spy_notifier, conn):
    conn.execute("INSERT INTO timers (duration_sec, due_ts, status, created_ts) "
                 "VALUES (60, 'not-a-date', 'running', '2026-11-15T10:00:00')")
    conn.commit()
    timer_store.add(60, "good", now=T0)
    await server._fire_due_timers(now=T0 + timedelta(minutes=2))
    assert any("good" in c.args[0] for c in spy_notifier.calls)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_server_timers.py -v`
Expected: FAIL — `Server` has no `_fire_due_timers`

- [ ] **Step 3: Implement `_fire_due_timers` and `_timer_tick` in `jarvisd/server.py`**

Mirror `_fire_due_reminders`/`_reminder_tick` exactly: sqlite and notify calls go through `asyncio.to_thread`, the tick wraps the body in `try/except` and logs, then sleeps. Give `_fire_due_timers` a `now` parameter defaulting to `datetime.now()` so it is testable. Start the task in the same place `_reminder_task` starts, guarded on `timer_store is not None and notifier is not None`, and cancel it in the same shutdown block. Spoken text: `f"Timer's up — {label}."` with a label, `"Timer's up."` without.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_server_timers.py tests/test_server.py tests/test_server_proactive.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add jarvisd/server.py tests/test_server_timers.py
git commit -m "feat: ring a due timer until the user dismisses it"
```

---

### Task 6: Timer tools

**Files:**
- Modify: `jarvisd/tools/reminders.py` — replace `set_timer`, add the new timer tools
- Test: `tests/test_tools_timer.py` (extend), `tests/test_tools_timer_control.py` (create)

**Interfaces:**
- Consumes: `TimerStore` (Task 2), `speak_duration` (Task 3).
- Produces: `handlers(reminder_store, timer_store=None)` — the second argument is optional so existing call sites keep working. New tool names:
  - `set_timer` — `{minutes: number, seconds?: number, label?: string}`, returns the timer id
  - `timer_status` — `{}`, lists live timers with spoken remaining time, plus timers fired in the last 30 minutes
  - `timer_control` — `{action: "pause"|"resume"|"extend"|"cancel"|"dismiss"|"snooze", timer_id?: integer, label?: string, minutes?: number}`

`timer_control` resolves its target by `timer_id`, else by `label`, else — when exactly one timer is live — that one. Ambiguity returns a message listing the candidates rather than guessing.

- [ ] **Step 1: Write the failing test**

```python
def test_set_timer_reports_its_id_so_it_can_be_controlled(h, store):
    out = h["set_timer"]({"minutes": 20})
    assert "#" in out and str(store.list_live(NOW)[0]["id"]) in out


def test_set_timer_accepts_seconds(h, store):
    h["set_timer"]({"minutes": 1, "seconds": 30})
    assert store.list_live(NOW)[0]["duration_sec"] == 90


def test_set_timer_rejects_nonpositive(h):
    assert "error" in h["set_timer"]({"minutes": 0}).lower()


def test_timer_status_speaks_remaining_time_not_a_timestamp(h, store):
    # The user asked "do I have a timer on right now?" and got nothing useful.
    h["set_timer"]({"minutes": 20, "label": "pasta"})
    out = h["timer_status"]({})
    assert "pasta" in out
    assert "minute" in out
    assert "T" not in out.split("pasta")[1][:40]      # no ISO timestamp


def test_timer_status_reports_a_timer_that_already_went_off(h, store):
    tid = store.add(60, "tea", now=NOW)
    store.mark_ringing(tid, NOW)
    out = h["timer_status"]({})
    assert "tea" in out and ("went off" in out.lower() or "ringing" in out.lower())


def test_timer_status_says_so_when_there_are_none(h):
    assert "no" in h["timer_status"]({}).lower()


def test_timer_status_lists_several_timers(h, store):
    h["set_timer"]({"minutes": 5, "label": "eggs"})
    h["set_timer"]({"minutes": 20, "label": "pasta"})
    out = h["timer_status"]({})
    assert "eggs" in out and "pasta" in out


def test_pause_and_resume_by_label(h, store):
    h["set_timer"]({"minutes": 10, "label": "tea"})
    assert "paused" in h["timer_control"]({"action": "pause", "label": "tea"}).lower()
    assert store.list_live(NOW)[0]["status"] == "paused"
    h["timer_control"]({"action": "resume", "label": "tea"})
    assert store.list_live(NOW)[0]["status"] == "running"


def test_extend_adds_minutes(h, store):
    h["set_timer"]({"minutes": 10, "label": "tea"})
    out = h["timer_control"]({"action": "extend", "label": "tea", "minutes": 5})
    assert "15 minutes" in out or "5" in out


def test_dismiss_stops_a_ringing_timer(h, store):
    tid = store.add(60, "tea", now=NOW)
    store.mark_ringing(tid, NOW)
    h["timer_control"]({"action": "dismiss", "label": "tea"})
    assert store.get(tid)["status"] == "done"


def test_snooze_a_ringing_timer(h, store):
    tid = store.add(60, "tea", now=NOW)
    store.mark_ringing(tid, NOW)
    h["timer_control"]({"action": "snooze", "label": "tea", "minutes": 9})
    assert store.get(tid)["status"] == "running"


def test_control_targets_the_only_timer_when_nothing_is_named(h, store):
    h["set_timer"]({"minutes": 10})
    assert "paused" in h["timer_control"]({"action": "pause"}).lower()


def test_control_asks_which_one_when_several_match(h, store):
    h["set_timer"]({"minutes": 10, "label": "tea"})
    h["set_timer"]({"minutes": 20, "label": "pasta"})
    out = h["timer_control"]({"action": "pause"})
    assert "tea" in out and "pasta" in out
    assert "which" in out.lower() or "several" in out.lower()


def test_control_on_an_unknown_label_does_not_raise(h):
    assert "no" in h["timer_control"]({"action": "pause", "label": "ghost"}).lower()


def test_extend_without_minutes_is_an_error(h, store):
    h["set_timer"]({"minutes": 10, "label": "tea"})
    assert "error" in h["timer_control"]({"action": "extend", "label": "tea"}).lower()


def test_an_unknown_action_is_an_error(h, store):
    assert "error" in h["timer_control"]({"action": "obliterate"}).lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_tools_timer_control.py -v`
Expected: FAIL — `KeyError: 'timer_control'`

- [ ] **Step 3: Implement the three handlers and their schemas in `jarvisd/tools/reminders.py`**

`set_timer` now writes to `timer_store`, not `reminder_store`. Keep returning a friendly confirmation that also carries the id. Keep the existing `minutes` validation (positive, not `bool`). Write the tool `description` strings so the model knows `timer_status` answers "is my timer done?" and that `timer_control` takes a label.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_tools_timer.py tests/test_tools_timer_control.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add jarvisd/tools/reminders.py tests/test_tools_timer.py tests/test_tools_timer_control.py
git commit -m "feat: timer_status and timer_control tools"
```

---

### Task 7: Stopwatch

**Files:**
- Create: `jarvisd/stopwatch_store.py`, `jarvisd/tools/stopwatch.py`
- Test: `tests/test_stopwatch_store.py`, `tests/test_tools_stopwatch.py`

**Interfaces:**
- Produces:
  - `class StopwatchStore(conn)` with `start(label="", now=None) -> int`, `pause(sw_id, now) -> bool`, `resume(sw_id, now) -> bool`, `stop(sw_id, now) -> int` (returns total seconds), `active(now) -> list[sqlite3.Row]`, and module-level `elapsed_sec(row, now) -> int`.
  - `jarvisd/tools/stopwatch.py` with `TOOLS` and `handlers(stopwatch_store)` exposing `stopwatch_control` — `{action: "start"|"pause"|"resume"|"stop"|"check", label?: string}`.

- [ ] **Step 1: Write the failing test**

```python
def test_elapsed_accumulates_across_a_pause(store):
    sid = store.start(now=T0)
    store.pause(sid, T0 + timedelta(seconds=30))
    store.resume(sid, T0 + timedelta(minutes=10))
    from jarvisd.stopwatch_store import elapsed_sec
    assert elapsed_sec(store.get(sid), T0 + timedelta(minutes=10, seconds=15)) == 45


def test_a_paused_stopwatch_does_not_keep_counting(store):
    sid = store.start(now=T0)
    store.pause(sid, T0 + timedelta(seconds=30))
    from jarvisd.stopwatch_store import elapsed_sec
    assert elapsed_sec(store.get(sid), T0 + timedelta(hours=3)) == 30


def test_stop_returns_the_total_and_retires_it(store):
    sid = store.start(now=T0)
    assert store.stop(sid, T0 + timedelta(seconds=90)) == 90
    assert store.active(T0) == []


def test_check_speaks_the_elapsed_time(h, store):
    store.start("run", now=T0)
    out = h["stopwatch_control"]({"action": "check"})
    assert "run" in out and "minute" in out or "second" in out


def test_check_with_no_stopwatch_does_not_raise(h):
    assert "no" in h["stopwatch_control"]({"action": "check"}).lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_stopwatch_store.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement both modules**

`elapsed_sec`: `accumulated_sec` plus, when running, `now - started_ts`. `pause` folds the running span into `accumulated_sec`; `resume` resets `started_ts`. Reuse `speak_duration` for output.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_stopwatch_store.py tests/test_tools_stopwatch.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add jarvisd/stopwatch_store.py jarvisd/tools/stopwatch.py tests/test_stopwatch_store.py tests/test_tools_stopwatch.py
git commit -m "feat: stopwatch store and tool"
```

---

### Task 8: Richer reminder recurrence

**Files:**
- Modify: `jarvisd/reminder_store.py` — `_VALID_RECURRENCES`, `_STEP`, `mark_fired`
- Modify: `jarvisd/tools/reminders.py` — `set_reminder` schema `enum`
- Test: `tests/test_reminder_store.py`

**Interfaces:**
- Produces: `_VALID_RECURRENCES = {"none", "daily", "weekdays", "weekends", "weekly", "monthly"}`; `_next_occurrence(due: datetime, recurrence: str, now: datetime) -> datetime` replaces the `while due_ts <= now: due_ts += step` loop in `mark_fired`.

- [ ] **Step 1: Write the failing test**

```python
import pytest
from datetime import datetime
from jarvisd.reminder_store import _next_occurrence

FRI = datetime(2026, 11, 13, 9, 0)      # Friday
SAT = datetime(2026, 11, 14, 9, 0)      # Saturday


def test_weekdays_skips_the_weekend():
    assert _next_occurrence(FRI, "weekdays", FRI).date() == datetime(2026, 11, 16).date()


def test_weekdays_created_on_a_saturday_lands_on_monday():
    assert _next_occurrence(SAT, "weekdays", SAT).date() == datetime(2026, 11, 16).date()


def test_weekends_skips_the_week():
    assert _next_occurrence(SAT, "weekends", SAT).date() == datetime(2026, 11, 15).date()
    assert _next_occurrence(FRI, "weekends", FRI).date() == datetime(2026, 11, 14).date()


def test_monthly_advances_one_month():
    d = datetime(2026, 1, 15, 9, 0)
    assert _next_occurrence(d, "monthly", d) == datetime(2026, 2, 15, 9, 0)


def test_monthly_on_the_31st_clamps_to_a_real_date():
    d = datetime(2026, 1, 31, 9, 0)
    nxt = _next_occurrence(d, "monthly", d)
    assert (nxt.month, nxt.day) == (2, 28)


def test_every_recurrence_terminates_and_lands_in_the_future():
    now = datetime(2026, 11, 15, 12, 0)
    past = datetime(2020, 1, 1, 9, 0)
    for rec in ("daily", "weekdays", "weekends", "weekly", "monthly"):
        assert _next_occurrence(past, rec, now) > now, rec


def test_set_reminder_schema_offers_the_new_recurrences():
    from jarvisd.tools.reminders import TOOLS
    schema = next(t for t in TOOLS if t["name"] == "set_reminder")
    enum = schema["input_schema"]["properties"]["recurrence"]["enum"]
    assert {"weekdays", "weekends", "monthly"} <= set(enum)


def test_an_invalid_recurrence_is_still_rejected(store):
    with pytest.raises(ValueError):
        store.add("x", "2026-12-01T09:00:00", "fortnightly")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_reminder_store.py -k "occurrence or recurrence" -v`
Expected: FAIL with `ImportError: cannot import name '_next_occurrence'`

- [ ] **Step 3: Implement `_next_occurrence` and widen the recurrence set**

Advance by one day for `weekdays`/`weekends`, skipping dates on the wrong side of the week, until strictly past `now`; one month for `monthly`, clamping the day to the target month's length. Keep `mark_fired`'s existing `BAD_DUE_TS` handling and its catch-up semantics (the result is always strictly after `now`).

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_reminder_store.py tests/test_tools_proactive.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add jarvisd/reminder_store.py jarvisd/tools/reminders.py tests/test_reminder_store.py
git commit -m "feat: weekdays, weekends and monthly recurrence"
```

---

### Task 9: Wire it all up

**Files:**
- Modify: `jarvisd/bootstrap.py` — build `TimerStore` and `StopwatchStore`, register both tool groups
- Modify: `jarvisd/tools/__init__.py:build_registry` — accept and wire `timer_store`, `stopwatch_store`
- Modify: `jarvisd/__main__.py` — pass `timer_store` to `Server`
- Modify: `security/policy.py` — tiers for the new tools
- Modify: `jarvisd/core.py` — the reminders/timers `CAPABILITIES` line
- Test: `tests/test_tools_registry.py`, `tests/test_policy_timer_tools.py`, `tests/test_bootstrap.py`, `tests/test_core_capabilities.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_policy_timer_tools.py
from security.policy import TOOL_PERMISSIONS


def test_timer_reads_are_read_only():
    assert TOOL_PERMISSIONS["timer_status"] == "read_only"


def test_no_timer_tool_is_destructive():
    # A confirmation prompt on "stop the timer" is the bug this plan prevents.
    for name in ("set_timer", "timer_status", "timer_control", "stopwatch_control"):
        assert TOOL_PERMISSIONS[name] in ("read_only", "write"), name


def test_every_new_tool_has_a_tier():
    for name in ("timer_status", "timer_control", "stopwatch_control"):
        assert name in TOOL_PERMISSIONS, f"{name} would fall back to DEFAULT_PERMISSION"
```

```python
# tests/test_tools_registry.py
def test_timer_tools_are_registered_when_a_timer_store_exists():
    tools, handlers = build_registry(..., timer_store=FakeTimerStore(), ...)
    names = {t["name"] for t in tools}
    assert {"set_timer", "timer_status", "timer_control"} <= names
    assert {"timer_status", "timer_control"} <= handlers.keys()


def test_timer_tools_are_absent_without_a_timer_store():
    tools, _ = build_registry(..., timer_store=None, ...)
    assert "timer_control" not in {t["name"] for t in tools}
```

```python
# tests/test_core_capabilities.py
def test_the_capability_line_describes_timer_control_when_available():
    prompt = render_system_prompt({"set_timer", "timer_status", "timer_control"})
    low = prompt.lower()
    assert "pause" in low and "snooze" in low


def test_no_timer_capability_line_without_the_tools():
    assert "snooze" not in render_system_prompt({"get_weather"}).lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_policy_timer_tools.py tests/test_tools_registry.py -v`
Expected: FAIL — `KeyError: 'timer_status'`

- [ ] **Step 3: Wire the stores, tools, tiers and prompt**

In `bootstrap.py`, build both stores from the same `conn` beside `ReminderStore` and return them the way `reminder_store` is returned, so `__main__` can hand `timer_store` to `Server`. Gate registration on the store existing, following the `if reminder_store:` pattern in `build_registry`. Add a `CAPABILITIES` entry gated on `("set_timer", "timer_status", "timer_control")` that tells the model timers can be named, paused, extended, snoozed and dismissed, and that `timer_status` is how to answer "is my timer done?".

- [ ] **Step 4: Run the whole suite**

Run: `python -m pytest tests/ -q`
Expected: PASS — all pre-existing tests plus the new ones

- [ ] **Step 5: Commit**

```bash
git add jarvisd/bootstrap.py jarvisd/tools/__init__.py jarvisd/__main__.py security/policy.py jarvisd/core.py tests/
git commit -m "feat: wire timer and stopwatch stores, tools, tiers and prompt"
```

---

### Task 10: End-to-end verification against the real daemon

**Files:** none (verification only)

- [ ] **Step 1: Run the full suite**

Run: `python -m pytest tests/ -q`
Expected: PASS, count >= the 1092 baseline plus the new tests

- [ ] **Step 2: Confirm the migration applies to the real database**

```bash
cp data/jarvis.db /tmp/jarvis-backup.db
python -c "from jarvisd import db; c=db.connect('data/jarvis.db'); print(c.execute('SELECT MAX(version) v FROM schema_version').fetchone()['v'])"
```
Expected: `11`, and the backup exists before anything touches the live file.

- [ ] **Step 3: Live-test a short timer with the daemon running**

Set a 1-minute timer by voice or CLI. Confirm in `logs/jarvisd.log` that the notify line records more than one route (spoken **and** local), that it re-rings about every 30s, and that it stops on "stop the timer". Then ask "do I have a timer running?" mid-countdown and confirm the answer names the label and the remaining time.

- [ ] **Step 4: Report the evidence**

Paste the relevant log lines. Use `superpowers:verification-before-completion` before claiming this works.
