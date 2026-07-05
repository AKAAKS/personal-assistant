# JARVIS v2 Phase 3 — Proactivity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** JARVIS acts without being spoken to first: reminders and recurring nudges that survive restarts, a single Notifier output path (spoken on a connected client → HA phone push fallback → queued for next chat; quiet hours respected), and Home Assistant event triggers (direct $0 rules + rate-limited smart rules). Spec §3 of `docs/superpowers/specs/2026-07-02-jarvis-v2-design.md`.

**Architecture:** DB migration v3 adds `reminders`, `notice_queue`, `trigger_rules`. New modules: `jarvisd/reminder_store.py`, `jarvisd/notifier.py`, `jarvisd/rules_store.py`, `jarvisd/ha_events.py`, `jarvisd/tools/reminders.py`, `jarvisd/tools/rules.py`. The server grows a connected-clients set, a scheduler asyncio task, and the HA event listener task. Clients get a concurrent receive loop so pushes can arrive at any time.

**Key deviation from spec:** no APScheduler. A 15-second asyncio tick over our own SQLite `reminders` table is dependency-free, restart-safe by construction, and trivially testable; APScheduler+SQLAlchemyJobStore would add two heavy deps for the same behavior. Natural-language time parsing stays in the model (tools take ISO datetimes), as the spec intends.

## Global Constraints

- Windows 11; Git Bash commands; working dir `C:/Users/akshi/Projects/personal-assistant`.
- Branch `phase3-proactivity` off master. Strict TDD per task (RED evidence → GREEN evidence).
- Never commit `.env`, `logs/`, `data/`. No new pip dependencies.
- Tests: `pytest` from repo root, no network, no sleeps longer than ~0.3s; time-dependent logic must accept an injectable `now` (callable or parameter) so tests never wait.
- **Every proactive action passes the guard + audit choke point.** Direct rules store their action AS a tool call (`{"tool": ..., "input": ...}`) and execute it through `Assistant`'s guarded path — never a raw `ha.call_service`. Smart rules go through `assistant.process()` like a user message.
- HA is frequently unreachable (it is down on this machine right now). The event listener must connect with exponential backoff (5s→60s cap), survive HA restarts, and be a silent no-op while HA is absent. Reminders/notifier must work with HA down (phone push just fails over to the queue).
- All times local, ISO `YYYY-MM-DDTHH:MM` granularity. DB timestamps `isoformat(timespec="seconds")` like Phase 2.
- WebSocket protocol addition (documented in `jarvisd/server.py` docstring): brain → client `{"type": "proactive_say", "text": str, "audio_follows": bool}` (+ optional WAV binary frame), can arrive at ANY time. Clients must tolerate unknown message types (forward compat).

---

### Task 0: Branch

- [ ] `git checkout -b phase3-proactivity`

---

### Task 1: Migration v3 + ReminderStore

**Files:** modify `jarvisd/db.py`; create `jarvisd/reminder_store.py`, `tests/test_reminder_store.py`.

Migration 3 (SCHEMA_VERSION = 3):

```sql
CREATE TABLE reminders (
  id INTEGER PRIMARY KEY,
  text TEXT NOT NULL,
  due_ts TEXT NOT NULL,
  recurrence TEXT NOT NULL DEFAULT 'none',   -- 'none' | 'daily' | 'weekly'
  status TEXT NOT NULL DEFAULT 'active',     -- 'active' | 'done' | 'cancelled'
  created_ts TEXT NOT NULL
);
CREATE TABLE notice_queue (
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,
  text TEXT NOT NULL,
  delivered INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE trigger_rules (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  event_type TEXT NOT NULL,                  -- HA event type, e.g. 'state_changed'
  entity_id TEXT,                            -- optional filter
  to_state TEXT,                             -- optional filter on new state
  kind TEXT NOT NULL,                        -- 'direct' | 'smart'
  action TEXT NOT NULL,                      -- direct: JSON {"tool":..., "input":...}; smart: instruction text
  cooldown_sec INTEGER NOT NULL DEFAULT 300,
  last_fired_ts TEXT,
  enabled INTEGER NOT NULL DEFAULT 1,
  created_ts TEXT NOT NULL
);
```

`ReminderStore(conn)`:
- `add(text, due_ts, recurrence="none") -> int` (validates recurrence + ISO due_ts parseable by `datetime.fromisoformat`, raises ValueError otherwise)
- `list_active() -> list[Row]` ordered by due_ts
- `cancel(reminder_id) -> bool` (False if not found / not active)
- `due(now: datetime) -> list[Row]` — active reminders with due_ts <= now
- `mark_fired(reminder_id, now: datetime)` — recurrence none → status 'done'; daily/weekly → advance due_ts by 1 day/7 days FROM ITS OWN due_ts until > now (catch-up loop so a reminder missed for 3 days fires once, not 3 times), status stays active.

- [ ] RED: tests for add/validate/list/cancel/due boundary (due_ts == now fires)/mark_fired for all three recurrences incl. multi-period catch-up; migration idempotent, v2 DB upgrades to v3.
- [ ] GREEN + full suite. Commit `feat: reminders/notices/rules schema + reminder store`.

---

### Task 2: Notifier

**Files:** create `jarvisd/notifier.py`, `tests/test_notifier.py`.

`Notifier(conn, ha_client=None, quiet_hours="23:00-08:00", now=datetime.now)`:
- `notify(text, priority="low")` — the single output path:
  - quiet hours active (parse "HH:MM-HH:MM", handles overnight wrap; empty string disables) → always enqueue.
  - `priority == "high"`: try `self.push_to_clients(text)` (a callable attribute set by the server, default None → returns 0 clients); if 0 clients reached and `ha_client` set → `ha_client.send_notification(text, title="JARVIS")` in try/except; if that also fails/absent → enqueue.
  - `priority == "low"` → enqueue.
- `drain() -> list[str]` — undelivered notices, marks them delivered, oldest first.
- Enqueue = INSERT into notice_queue. All failures logged, never raised.

- [ ] RED: quiet-hours parsing (overnight wrap 23:00-08:00 at 23:30 and 07:59 = quiet, 12:00 = not; empty = never quiet); high→clients when callable reports >0; high→HA fallback when 0 clients; high→queue when both unavailable; low→queue; drain marks delivered and is idempotent. Inject `now` for every time test.
- [ ] GREEN + full suite. Commit `feat: notifier — single proactive output path with quiet hours`.

---

### Task 3: Reminder + rule tools, policy, registry

**Files:** create `jarvisd/tools/reminders.py`, `jarvisd/rules_store.py`, `jarvisd/tools/rules.py`, `tests/test_rules_store.py`, `tests/test_tools_proactive.py`; modify `jarvisd/tools/__init__.py`, `security/policy.py`, `tests/test_tools_registry.py`.

`RulesStore(conn)`: `add(name, event_type, kind, action, entity_id=None, to_state=None, cooldown_sec=300) -> int` (kind validated; direct action must be valid JSON with "tool"+"input" keys — validate at add time), `list_enabled()`, `list_all()`, `delete(rule_id) -> bool`, `matches(event: dict) -> list[Row]` (event_type equal; entity_id/to_state filters when set — event shape: `{"event_type", "entity_id", "to_state"}`), `check_and_stamp_cooldown(rule_id, now) -> bool` (True = allowed to fire; stamps last_fired_ts).

Tools (follow `jarvisd/tools/memory.py` pattern):
- `set_reminder` {text, due (ISO datetime), recurrence?} → "Reminder #N set for {due}" / ValueError message on bad input. **write**
- `list_reminders` {} → numbered lines or "No active reminders." **read_only**
- `cancel_reminder` {reminder_id: int} → confirmation / not-found message. **write**
- `add_trigger_rule` {name, event_type, kind, action, entity_id?, to_state?, cooldown_sec?} → "Rule #N '{name}' added." Tool description must explain both kinds with an example each (direct action = JSON string of a tool call, e.g. `{"tool": "control_light", "input": {"entity_id": "light.hall", "action": "turn_on"}}`). **write**
- `list_trigger_rules` {} **read_only**
- `delete_trigger_rule` {rule_id: int} **write**

Registry: `build_registry(..., reminder_store=None, rules_store=None)` — reminder tools when reminder_store set, rule tools when rules_store set, appended after memory tools in that order (deterministic).

- [ ] RED → GREEN → full suite. Commit `feat: reminder + trigger-rule tools wired into registry + policy`.

---

### Task 4: Server — client registry, proactive push, scheduler tick, notice drain

**Files:** modify `jarvisd/server.py`, `jarvisd/core.py` (one small addition), `tests/test_server_proactive.py` (new), `tests/test_server.py` (only if a fixture needs the new constructor args — do not weaken existing assertions).

1. `JarvisServer.__init__` gains `notifier=None, reminder_store=None, assistant_lock_holder=self` — keep backward compatible defaults. Track clients: `self._clients: set = set()`; `handle()` adds/discards the websocket in a try/finally.
2. `push_proactive(text)` (async): synthesize TTS (if available) off-thread, send `proactive_say` JSON (+WAV) to every connected client, return count reached. Wire `notifier.push_to_clients` to a **thread-safe bridge**: the Notifier runs inside worker threads and the scheduler task — use `asyncio.run_coroutine_threadsafe(self.push_proactive(text), self._loop).result(timeout=10)` where `self._loop` is captured in `serve_forever()`. Guard for "loop not running yet" → return 0.
3. Scheduler task: `async def _reminder_tick()` — every 15s: `due = reminder_store.due(datetime.now())`; for each: `notifier.notify(f"Reminder: {r['text']}", priority="high")`, `mark_fired`. Wrap the whole tick body in try/except (a bad reminder must not kill the loop). Started in `serve_forever()` via `asyncio.create_task` only when reminder_store and notifier are both set.
4. Notice drain: in `_handle_json` text_in path and `_handle_audio` path, after obtaining `reply`, call `notices = notifier.drain()` (if notifier) and if non-empty, `reply = reply + "\n\nWhile you were away:\n" + "\n".join("- " + n for n in notices)`. Drain AFTER the agent call so recall/persistence see the raw reply.
5. `core.py` addition: none beyond what exists — confirm nothing needed; if the implementer finds otherwise, STOP and report.

- [ ] RED: loopback tests with fake stores/notifier — proactive_say reaches a connected client (JSON shape incl. audio_follows False when tts None); push returns 0 with no clients; reminder tick fires a due reminder exactly once and advances recurrence (inject a controllable tick by calling the tick body function directly rather than waiting 15s — factor the body into `_fire_due_reminders()` sync-callable-from-test via asyncio.run or make it an async method tested with asyncio.run); drained notices appended to reply.
- [ ] GREEN + full suite. Commit `feat: server scheduler tick, proactive push, notice drain`.

---

### Task 5: Clients — concurrent receive loop

**Files:** modify `clients/voice_client.py`, `clients/cli.py`; create `tests/test_clients_protocol.py` (loopback test with a fake server pushing proactive_say between replies).

Both clients restructure from lockstep send→recv to: one background receiver task that dispatches every incoming message (reply / proactive_say / error / unknown-type tolerated; binary frame = WAV for the most recent audio_follows), while the send side awaits its reply via an asyncio.Queue fed by the receiver (replies go to the queue; proactive_say is handled immediately: print `JARVIS (proactive): {text}` and play audio in voice client).
- Voice client: wake-word capture loop unchanged; audio playback serialized (one winsound.PlaySound at a time — a proactive WAV must not interleave with a reply WAV; use an asyncio.Lock around playback).
- CLI: proactive lines print above the prompt; input loop unchanged otherwise (`asyncio.to_thread(input, ...)` pattern if not already).
- Keep the existing reconnect/error messages from Phase 1 intact.

- [ ] RED: loopback fake server sends reply, then unsolicited proactive_say, then reply — client receives both replies in order and the proactive handler fired (capture via injected callback/monkeypatch print). Unknown message type ignored without crash.
- [ ] GREEN + full suite. Commit `feat: clients handle proactive pushes concurrently`.

---

### Task 6: HA event listener + rule execution through the guard

**Files:** create `jarvisd/ha_events.py`, `tests/test_ha_events.py`; modify `jarvisd/server.py` (start listener task), `jarvisd/core.py` (expose `execute_guarded(tool, input) -> str` — a thin public wrapper around the existing guard.check → audit → _execute_tool sequence, factored out of `_claude_loop` so BOTH paths share one implementation; `_claude_loop` must call the same wrapper).

`HAEventListener(ha_url, ha_token, rules_store, notifier, run_direct, run_smart)`:
- Async task: connect to HA WebSocket (`ws://host:port/api/websocket` derived from the http base_url), auth handshake (`auth_required` → send `{"type":"auth","access_token":...}` → `auth_ok`), `subscribe_events` for `state_changed`. Reconnect with exponential backoff 5s→60s; never raises out of the task; INFO log connect/disconnect once per transition (no log spam per retry — log at most every backoff step).
- On event: reduce to `{"event_type", "entity_id", "to_state"}` (from `event.data.entity_id` / `event.data.new_state.state`), find matching enabled rules, for each passing `check_and_stamp_cooldown`:
  - direct: `run_direct(json.loads(rule["action"]))` → calls `assistant.execute_guarded(action["tool"], action["input"])` via `asyncio.to_thread` under the SAME assistant lock the server uses; notify low-priority summary `f"Rule '{name}': {result}"`.
  - smart: `run_smart(f"[Automation rule '{name}' triggered by {entity_id} -> {to_state}] {rule['action']}")` → `assistant.process(...)` via to_thread under the lock; notifier.notify(result, "high").
- `run_direct`/`run_smart` are injected callables created in `server.py`/`__main__` wiring so the listener stays unit-testable without a real assistant.

- [ ] RED: unit tests with a fake WS transport (feed the handshake + a state_changed event as dicts through the message-processing function — factor `_process_event(event)` so tests never open sockets): matching/filtering, cooldown blocks second fire, direct action goes through a spy `run_direct` with parsed JSON, smart through `run_smart` with the composed prompt, malformed rule action JSON → logged + skipped, listener no-op when rules_store empty. Test `execute_guarded` in `tests/test_core_memory.py`-style: BLOCK verdict short-circuits (spy handler not called) and audit logged — and `_claude_loop` still enforces guard via the shared wrapper (existing guard tests must stay green).
- [ ] GREEN + full suite. Commit `feat: HA event triggers — direct + smart rules through the guard choke point`.

---

### Task 7: Wiring + README + ledger

**Files:** modify `jarvisd/bootstrap.py`, `jarvisd/__main__.py`, `README.md`.

- Bootstrap: build `ReminderStore`, `RulesStore` on the same conn (inside the existing memory try/except — all-or-nothing with memory), pass to `build_registry` via Assistant; return them (extend the return or attach to assistant, implementer picks the cleanest minimal change and documents it).
- `__main__`: construct `Notifier(conn, ha_client, quiet_hours=os.getenv("QUIET_HOURS", "23:00-08:00"))`, hand notifier + reminder_store to `JarvisServer`, start HA listener only when HA token present. "Proactivity" added to features line when wired.
- README: Proactivity section — reminder phrasing examples, rule examples (one direct, one smart), quiet hours env, note that HA-down is tolerated.
- [ ] Full suite + `python -c "from jarvisd.bootstrap import create_assistant"` import check. Commit `feat: wire proactivity into daemon + README`.

---

## Final acceptance checklist (manual)

1. `pytest` all green.
2. "Remind me in 2 minutes to stretch" → tool sets reminder; ~2 min later the connected voice client speaks it (or phone push if no client); `logs/audit.jsonl` shows the flow.
3. "What are my reminders?" lists it; "cancel reminder 1" cancels.
4. With no client connected and quiet hours off: reminder arrives as HA phone push (requires HA up) or appears under "While you were away" on next chat.
5. Set QUIET_HOURS to include now; fire a reminder; it queues instead of speaking.
6. (Requires HA up) create a direct rule conversationally; trip the entity; action executes and audit shows the guarded call.
