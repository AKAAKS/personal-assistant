# Claude Code Status Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the user ask JARVIS "is Claude still going?" and be told, unprompted, when a Claude Code session has sat unattended — working, waiting on a permission prompt, or finished with what it said.

**Architecture:** Claude Code hooks fire a thin stdin adapter that writes one small JSON record per session under `data/claude_sessions/`. A shared module owns the record schema, the atomic write, and liveness. A read-only `claude_status` tool summarizes those records on demand; a daemon tick announces the ones Claude Code's own idle signal has flagged, through the proactive notifier path.

**Tech Stack:** Python 3.11, stdlib (`json`, `os`, `tempfile`, `time`), `psutil` (already a dependency), pytest. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-11-12-claude-code-status-design.md`

## Global Constraints

- Windows 11; the daemon runs under `pythonw` with no console. Claude Code 2.1.323.
- Every hook entry in settings is `async: true`. The hook exits 0 always and writes **nothing** to stdout or stderr — Claude Code interprets hook stdout on some events.
- **The hook is the only writer of the state directory.** The daemon and the tool only read. Announcement dedupe lives in daemon memory, never on disk.
- Every field of a hook payload is read with `.get()`; no field is required to be present.
- All paths derived from `__file__`, never from the process cwd (the hook inherits the Claude session's cwd, which varies).
- `data/` is gitignored; no state file is ever committed.
- `MESSAGE_CAP = 500` characters for `last_message`. `DONE_REPORT_SEC = 1800` (finished sessions older than 30 minutes are counted, not quoted). `NO_PID_TTL_SEC = 43200` (12h fallback when no `claude.exe` ancestor is found).
- `claude_status` is `read_only` in `security/policy.py` and registered unconditionally in `build_registry`.
- TDD throughout: test first, watch it fail, minimal code, watch it pass, commit.

## Review Focus

Input classes the spec implies but which no task's happy path exercises. Each has a test added to the task that owns the code.

1. **A corrupt or truncated record on disk** (hand-edited, interrupted write) must be skipped, not crash the tool or the daemon tick — Task 1.
2. **Empty or non-JSON stdin to the hook**, or JSON that is not an object, must exit 0 and write nothing — Task 3.
3. **An unknown `hook_event_name`** (a newer Claude Code event, or one we never registered) must be ignored without creating a junk record — Task 1.
4. **Two concurrent sessions in the same project** must be reported distinguishably rather than merged or silently deduped — Task 4.
5. **A `last_message` containing newlines, markdown or non-ASCII** must not break the spoken summary — Task 4.

---

### Task 1: The session record store

**Files:**
- Create: `jarvisd/claude_sessions.py`
- Test: `tests/test_claude_sessions.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `WORKING = "working"`, `WAITING = "waiting"`, `DONE = "done"`; `DIR` (absolute `Path` to `data/claude_sessions`, derived from `__file__`); `MESSAGE_CAP = 500`; `apply_event(payload: dict, *, directory=DIR, now=time.time) -> None`; `read_raw(*, directory=DIR) -> list[dict]`.

Module docstring must say why this file exists: it is the one owner of the record schema, shared by a hook process and the daemon, and the hook is its only writer.

`apply_event` routes on `payload.get("hook_event_name")`:

| Event | Effect |
|---|---|
| `SessionStart` | create record, `state = WORKING` |
| `UserPromptSubmit` | `state = WORKING`, `last_message = None` |
| `PermissionRequest` | `state = WAITING`, `tool_name = payload.get("tool_name")` |
| `Notification` | `state = WAITING` if the message/matcher looks like a permission prompt; otherwise set `idle_ts` only |
| `Stop` | `state = DONE`, `last_message = payload.get("last_assistant_message")` truncated to `MESSAGE_CAP` |
| `SessionEnd` | delete the record file |
| anything else | return without writing |

`since` updates **only** when `state` changes value. `idle_ts` never changes `state`.

- [ ] **Step 1: Write the failing tests**

```python
def test_session_start_creates_a_working_record(tmp_path):
    cs.apply_event({"hook_event_name": "SessionStart", "session_id": "s1",
                    "cwd": r"C:\p\personal-assistant"}, directory=tmp_path)
    rec = cs.read_raw(directory=tmp_path)[0]
    assert rec["state"] == cs.WORKING
    assert rec["project"] == "personal-assistant"
    assert rec["session_id"] == "s1"

def test_stop_records_the_last_message_truncated(tmp_path):
    _start(tmp_path)
    cs.apply_event({"hook_event_name": "Stop", "session_id": "s1",
                    "last_assistant_message": "x" * 900}, directory=tmp_path)
    rec = cs.read_raw(directory=tmp_path)[0]
    assert rec["state"] == cs.DONE
    assert len(rec["last_message"]) == cs.MESSAGE_CAP

def test_idle_notification_does_not_overwrite_done(tmp_path):
    """The first draft's bug: idle 60s after Stop turned a finished session
    into 'waiting on you' and destroyed the message it had just recorded."""
    _start(tmp_path); _stop(tmp_path, "pushed the commit")
    cs.apply_event({"hook_event_name": "Notification", "session_id": "s1",
                    "message": "Claude is waiting for your input"}, directory=tmp_path)
    rec = cs.read_raw(directory=tmp_path)[0]
    assert rec["state"] == cs.DONE
    assert rec["last_message"] == "pushed the commit"
    assert rec["idle_ts"] is not None

def test_permission_request_waits_and_names_the_tool(tmp_path): ...
def test_a_new_prompt_clears_the_previous_last_message(tmp_path): ...
def test_since_only_moves_when_the_state_changes(tmp_path):
    # two UserPromptSubmit events at different clocks -> `since` unchanged
def test_session_end_deletes_the_record(tmp_path): ...
def test_an_unknown_event_writes_nothing(tmp_path):
    cs.apply_event({"hook_event_name": "WorktreeCreate", "session_id": "s9"},
                   directory=tmp_path)
    assert cs.read_raw(directory=tmp_path) == []
def test_a_corrupt_record_is_skipped_not_raised(tmp_path):
    (tmp_path / "bad.json").write_text("{not json", encoding="utf-8")
    assert cs.read_raw(directory=tmp_path) == []
def test_a_missing_payload_field_never_raises(tmp_path):
    cs.apply_event({"hook_event_name": "SessionStart"}, directory=tmp_path)
def test_the_write_leaves_no_temp_file_behind(tmp_path): ...
def test_the_state_directory_is_absolute(): 
    assert cs.DIR.is_absolute()
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m pytest tests/test_claude_sessions.py -q`
Expected: FAIL — `No module named 'jarvisd.claude_sessions'`

- [ ] **Step 3: Implement `jarvisd/claude_sessions.py`**

Atomic write exactly as `clients/activity.py:write` does it (`tempfile.mkstemp` in the target directory, `json.dump`, `os.replace`, unlink the temp on failure). `apply_event` reads the existing record first (missing or corrupt reads as a fresh one), applies the event, writes it back. Nothing in the module raises: every public function wraps its body and logs at debug.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_claude_sessions.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add jarvisd/claude_sessions.py tests/test_claude_sessions.py
git commit -m "feat: record Claude Code session state, one atomic file per session"
```

---

### Task 2: Liveness, so a dead session stops claiming to be busy

**Files:**
- Modify: `jarvisd/claude_sessions.py`
- Test: `tests/test_claude_sessions_liveness.py`

**Interfaces:**
- Consumes: Task 1's module.
- Produces: `claude_ancestor(*, start_pid=None, walker=...) -> tuple[int, float] | None`; `is_live(record, *, now=time.time, probe=...) -> bool`; `read_sessions(*, directory=DIR, now=time.time) -> list[dict]` (live records only, newest state change first); `NO_PID_TTL_SEC = 43200`.

Why this is not a TTL: a session can sit on a permission prompt for hours, so an activity-style TTL would expire a state that is true. `os.getppid()` is not usable either — verified ancestry from a hook-like child on this machine is `bash → bash → bash → claude.exe → powershell → WindowsTerminal`, so the hook's parent is a transient shell. `claude_ancestor` walks up with `psutil` for a process named `claude.exe` and records its pid **and** `create_time()`; the create time is what defeats pid reuse.

`apply_event` stores `claude_pid` and `claude_started` on every write. A record with no pid falls back to `NO_PID_TTL_SEC` against `updated_ts`.

- [ ] **Step 1: Write the failing tests**

```python
def test_finds_claude_in_the_ancestor_chain_not_just_the_parent():
    chain = [(10, "python.exe"), (11, "cmd.exe"), (12, "claude.exe"), (13, "powershell.exe")]
    assert cs.claude_ancestor(walker=_fake_walker(chain))[0] == 12

def test_no_claude_ancestor_returns_none(): ...
def test_a_record_whose_pid_is_gone_is_not_live(): ...
def test_a_reused_pid_with_a_different_start_time_is_not_live():
    """Same pid, different create_time -> an unrelated process; the session is gone."""
def test_a_live_matching_pid_is_live_however_old_the_record(): ...
def test_a_record_with_no_pid_expires_after_the_ttl(): ...
def test_a_record_with_no_pid_survives_inside_the_ttl(): ...
def test_read_sessions_drops_dead_records(tmp_path): ...
def test_read_sessions_orders_by_most_recent_state_change(tmp_path): ...
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m pytest tests/test_claude_sessions_liveness.py -q`
Expected: FAIL — `module has no attribute 'claude_ancestor'`

- [ ] **Step 3: Implement `claude_ancestor`, `is_live` and `read_sessions`**

`walker` and `probe` are injected so the tests never need a real process tree; their defaults use `psutil`. A missing or dead `psutil` lookup resolves to "not live" only when the pid is genuinely absent — a `psutil.AccessDenied` must read as live, since a process we cannot inspect is still running.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_claude_sessions_liveness.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add jarvisd/claude_sessions.py tests/test_claude_sessions_liveness.py
git commit -m "feat: expire a Claude session record by process liveness, not a timer"
```

---

### Task 3: The hook script

**Files:**
- Create: `scripts/claude_status_hook.py`
- Test: `tests/test_claude_status_hook.py`

**Interfaces:**
- Consumes: `jarvisd.claude_sessions.apply_event`.
- Produces: a module with `main(stdin, *, directory=None) -> int` returning 0 always, invoked under `if __name__ == "__main__"`.

The script is a thin adapter and nothing else: read stdin, parse, call `apply_event`, return 0. It must add the repo root to `sys.path` (derived from `__file__`) because Claude Code invokes it by absolute path from an arbitrary cwd.

- [ ] **Step 1: Write the failing tests**

```python
def test_a_valid_event_reaches_the_store(tmp_path):
    payload = json.dumps({"hook_event_name": "SessionStart", "session_id": "s1", "cwd": "/x/proj"})
    assert hook.main(io.StringIO(payload), directory=tmp_path) == 0
    assert cs.read_raw(directory=tmp_path)[0]["state"] == cs.WORKING

def test_empty_stdin_exits_zero_and_writes_nothing(tmp_path):
    assert hook.main(io.StringIO(""), directory=tmp_path) == 0
    assert cs.read_raw(directory=tmp_path) == []

def test_non_json_stdin_exits_zero(tmp_path): ...
def test_json_that_is_not_an_object_exits_zero(tmp_path):
    assert hook.main(io.StringIO("[1,2,3]"), directory=tmp_path) == 0

def test_a_store_failure_still_exits_zero(monkeypatch, tmp_path):
    """A broken status write must never fail a Claude Code turn."""
    monkeypatch.setattr(cs, "apply_event", _raiser)
    assert hook.main(io.StringIO('{"hook_event_name":"Stop"}'), directory=tmp_path) == 0

def test_it_writes_nothing_to_stdout_or_stderr(capsys, tmp_path):
    hook.main(io.StringIO('{"hook_event_name":"SessionStart","session_id":"s1"}'),
              directory=tmp_path)
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m pytest tests/test_claude_status_hook.py -q`
Expected: FAIL — module not found

- [ ] **Step 3: Implement `scripts/claude_status_hook.py`**

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_claude_status_hook.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/claude_status_hook.py tests/test_claude_status_hook.py
git commit -m "feat: add the Claude Code status hook adapter"
```

---

### Task 4: The `claude_status` tool

**Files:**
- Create: `jarvisd/tools/dev_status.py`
- Modify: `jarvisd/tools/__init__.py` (import, `TOOLS`, `handlers()` — unconditional, beside `clipboard`)
- Modify: `security/policy.py` (`"claude_status": "read_only"`)
- Modify: `jarvisd/core.py` (one `CAPABILITIES` entry gated on `("claude_status",)`)
- Test: `tests/test_tools_claude_status.py`

**Interfaces:**
- Consumes: `read_sessions`, `DIR`, state constants, `DONE_REPORT_SEC`.
- Produces: `TOOLS` (one definition, no input properties); `handlers(*, directory=None, now=time.time) -> {"claude_status": callable}`; `summarize(records, *, now) -> str`.

Three answers that must stay distinct, because the spec's second anti-goal is confident wrong answers:

- state directory **missing** → "The Claude Code status hooks aren't installed yet." (never "nothing is running")
- directory present, no live records → "No Claude Code sessions are running."
- live records → one clause each: project, state, how long, and for a session finished within `DONE_REPORT_SEC` its `last_message`. Older finished sessions are counted only.

- [ ] **Step 1: Write the failing tests**

```python
def test_a_missing_state_dir_says_the_hooks_are_not_installed(tmp_path):
    out = _call(directory=tmp_path / "nope")
    assert "install" in out.lower()

def test_an_empty_state_dir_says_nothing_is_running(tmp_path):
    assert "no claude code session" in _call(directory=tmp_path).lower()

def test_a_working_session_names_the_project_and_the_duration(tmp_path): ...
def test_a_waiting_session_says_it_needs_you(tmp_path): ...
def test_a_recently_finished_session_quotes_what_it_said(tmp_path):
    _done(tmp_path, "pushed the commit", age=60)
    assert "pushed the commit" in _call(directory=tmp_path)

def test_an_old_finished_session_is_counted_not_quoted(tmp_path):
    _done(tmp_path, "pushed the commit", age=cs_tool.DONE_REPORT_SEC + 60)
    out = _call(directory=tmp_path)
    assert "pushed the commit" not in out

def test_two_sessions_in_the_same_project_are_both_reported(tmp_path):
    """Review Focus 4: same cwd must not merge or silently drop one."""
    _working(tmp_path, "s1", project="proj"); _waiting(tmp_path, "s2", project="proj")
    out = _call(directory=tmp_path)
    assert out.count("proj") >= 2

def test_a_markdown_and_multiline_message_is_flattened(tmp_path):
    """Review Focus 5: this text is spoken, so it must survive formatting."""
    _done(tmp_path, "**bold**\n\n- a\n- b", age=10)
    out = _call(directory=tmp_path)
    assert "\n\n" not in out and "**" not in out

def test_the_tool_is_read_only_in_the_policy():
    assert TOOL_PERMISSIONS["claude_status"] == "read_only"

def test_the_tool_is_registered_unconditionally():
    tools, handlers = build_registry(ha_client=None, system_client=None)
    assert "claude_status" in handlers
    assert any(t["name"] == "claude_status" for t in tools)

def test_the_prompt_advertises_it():
    assert any("claude_status" in names for _, names in CAPABILITIES)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m pytest tests/test_tools_claude_status.py -q`
Expected: FAIL — module not found

- [ ] **Step 3: Implement the tool and its three wiring points**

Reuse `jarvisd.text_format.strip_markdown` for the message, and collapse whitespace — the result is read aloud. The tool description must tell the model this answers questions about *Claude Code sessions on the PC*, so it is not chosen for "what are you thinking".

- [ ] **Step 4: Run the tests and the full suite**

Run: `python -m pytest tests/test_tools_claude_status.py -q` then `python -m pytest -q`
Expected: PASS; the 3 pre-existing `serve_forever` port-8765 failures remain when the daemon is running.

- [ ] **Step 5: Commit**

```bash
git add jarvisd/tools/dev_status.py jarvisd/tools/__init__.py security/policy.py jarvisd/core.py tests/test_tools_claude_status.py
git commit -m "feat: add the claude_status tool"
```

---

### Task 5: Perishable notices

**Files:**
- Modify: `jarvisd/notifier.py`
- Test: `tests/test_notifier_perishable.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `Notifier.notify(text, priority="low", *, perishable=False) -> None`.

A perishable notice is never queued: during quiet hours, or when no route is available, it is dropped with a log line instead of banked. Banking it is the fourth anti-goal — "Claude has been waiting on you" delivered at 8am about a session that ended at midnight. Keyword-only so all existing positional call sites are untouched.

- [ ] **Step 1: Write the failing tests**

```python
def test_a_perishable_notice_is_dropped_in_quiet_hours(): 
    # quiet window active, priority high -> notice_queue unchanged
def test_a_perishable_notice_is_dropped_when_no_route_delivers(): ...
def test_a_perishable_notice_still_speaks_when_a_client_is_connected(): ...
def test_a_perishable_notice_still_falls_back_to_the_local_toast(): ...
def test_a_normal_notice_still_queues(): 
    """The default must not change: reminders keep banking."""
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m pytest tests/test_notifier_perishable.py -q`
Expected: FAIL — unexpected keyword argument `perishable`

- [ ] **Step 3: Add the `perishable` keyword to `notify`**

- [ ] **Step 4: Run the notifier tests**

Run: `python -m pytest tests/test_notifier_local.py tests/test_notifier_perishable.py tests/test_timer_urgency.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add jarvisd/notifier.py tests/test_notifier_perishable.py
git commit -m "feat: let a notice be perishable rather than banked"
```

---

### Task 6: The announcement tick

**Files:**
- Modify: `jarvisd/server.py` (constant, `__init__`, `serve_forever`, two new methods)
- Test: `tests/test_claude_watch.py`

**Interfaces:**
- Consumes: `read_sessions`, state constants, `Notifier.notify(..., perishable=True)`.
- Produces: `CLAUDE_WATCH_TICK_SEC = 15`; `JarvisServer.announcement_for(record) -> str | None` (static, pure); `JarvisServer._claude_watch_tick()` (its own asyncio task, started in `serve_forever` beside `_reminder_task`, cancelled in the same `finally`).

Its own task rather than folded into `_reminder_tick`: a failure in one must not take the other down.

Announce only when `idle_ts` is set **and** `idle_ts >= since`. Without that guard a stale idle from an earlier state would announce a finish the user is sitting in front of. Dedupe in memory on `(session_id, state, since)` — a daemon restart may re-announce a session that is *still* waiting, which is acceptable.

Wording, from the spec:

| State | Announcement |
|---|---|
| `WAITING` | "Claude needs your input in `<project>`." |
| `DONE` | "Claude finished in `<project>`." + the message |
| `WORKING` | `None` — a long turn is not news |

- [ ] **Step 1: Write the failing tests**

```python
def test_a_waiting_session_announces_that_it_needs_you(): ...
def test_a_finished_session_announces_what_it_said(): ...
def test_a_working_session_announces_nothing():
    assert JarvisServer.announcement_for(_rec(cs.WORKING, idle=True)) is None

def test_no_idle_signal_announces_nothing():
    """Being at the keyboard must stay silent; idle_prompt is the debounce."""
    assert JarvisServer.announcement_for(_rec(cs.WAITING, idle=False)) is None

def test_a_stale_idle_from_a_previous_state_announces_nothing():
    # idle_ts < since -> the state moved on after the idle fired
def test_each_transition_announces_once(): 
    # two ticks over the same record -> one notify call
def test_a_new_state_announces_again(): 
    # waiting -> working -> waiting produces a second notify
def test_the_announcement_is_perishable():
    # notify called with perishable=True
def test_a_tick_failure_does_not_kill_the_loop(): ...
def test_serve_forever_starts_and_cancels_the_watch_task(): ...
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m pytest tests/test_claude_watch.py -q`
Expected: FAIL — `JarvisServer has no attribute 'announcement_for'`

- [ ] **Step 3: Implement the constant, `announcement_for`, `_claude_watch_tick`, and the `serve_forever` wiring**

`announcement_for` is static and pure so every wording and guard case is tested without a server, a clock, or a notifier. The tick wraps each pass in `try/except` and logs, mirroring `_reminder_tick`.

- [ ] **Step 4: Run the tests and the full suite**

Run: `python -m pytest tests/test_claude_watch.py -q` then `python -m pytest -q`
Expected: PASS apart from the 3 known port-8765 failures.

- [ ] **Step 5: Commit**

```bash
git add jarvisd/server.py tests/test_claude_watch.py
git commit -m "feat: announce an unattended Claude Code session"
```

---

### Task 7: The hook installer

**Files:**
- Create: `scripts/install_claude_hooks.py`
- Test: `tests/test_install_claude_hooks.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `hook_entries(script_path: str) -> dict` (the `hooks` fragment for the six events); `install(settings_path, *, script_path, backup=True) -> str` (returns a human-readable summary).

This edits a file outside the repo that holds the user's own configuration, so: **merge, never overwrite**; back it up first; idempotent (running twice leaves one copy); and it must preserve every unrelated key — the user's `permissions`, `model`, `enabledPlugins`, `mcpServers` and the rest.

- [ ] **Step 1: Write the failing tests**

```python
def test_it_registers_all_six_events(tmp_path): 
    # SessionStart, UserPromptSubmit, PermissionRequest, Notification, Stop, SessionEnd
def test_every_entry_is_async(tmp_path):
    """A synchronous hook would make JARVIS able to stall a Claude turn."""
def test_it_preserves_unrelated_settings_keys(tmp_path):
    # permissions/model/mcpServers survive untouched
def test_it_merges_into_an_existing_hooks_block(tmp_path): ...
def test_running_twice_does_not_duplicate_entries(tmp_path): ...
def test_it_writes_a_backup_first(tmp_path): ...
def test_it_refuses_rather_than_clobbering_unparseable_settings(tmp_path):
    """Better to stop than to replace a config we could not read."""
def test_the_script_path_is_absolute(tmp_path): ...
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m pytest tests/test_install_claude_hooks.py -q`
Expected: FAIL — module not found

- [ ] **Step 3: Implement the installer**

`install` takes `settings_path` so the tests never touch the real `~/.claude/settings.json`; `__main__` defaults it to `Path.home() / ".claude" / "settings.json"` and prints the summary.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_install_claude_hooks.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/install_claude_hooks.py tests/test_install_claude_hooks.py
git commit -m "feat: add an idempotent installer for the status hooks"
```

---

### Task 8: Install, verify live, document

**Files:**
- Modify: `README.md` (test count; one line in the PC-control/capability list)
- Modify: `docs/superpowers/specs/2026-11-12-claude-code-status-design.md` (status → implemented, and record which events actually fired)

- [ ] **Step 1: Run the installer against the real settings file**

Run: `python scripts/install_claude_hooks.py`
Expected: a backup written, six events registered, unrelated keys intact. Confirm by reading `~/.claude/settings.json` and diffing the key set against the backup.

- [ ] **Step 2: Probe which events actually fire**

The payload shapes for `Notification` and `PermissionRequest` are undocumented and an `async` hook may not run for a decision-point event. In a scratch Claude Code session: submit a prompt, trigger a permission prompt, let a turn finish, go idle ~60s, then end the session. After each, list `data/claude_sessions/` and record which events produced which fields.
Expected: at minimum `UserPromptSubmit` and `Stop` produce records. Whatever does not fire is noted in the design doc as degraded, not patched around.

- [ ] **Step 3: Verify the pull path end to end**

Ask JARVIS "is Claude still going?" through the voice client while a session is working, then while one is finished.
Expected: the answer names the project and the state, and the finished one quotes its message.

- [ ] **Step 4: Verify the announcement end to end**

Leave a session at a permission prompt and walk away for ~90s.
Expected: `logs/jarvisd.log` shows `notify[high] -> spoken to 1 client(s)` (the route logging added earlier today) and the announcement is audible.

- [ ] **Step 5: Update the docs and commit**

```bash
git add README.md docs/superpowers/specs/2026-11-12-claude-code-status-design.md
git commit -m "docs: record Claude Code status as shipped and which hooks fire"
```

---

## Self-review notes

- **Spec coverage:** states and the idle correction (Task 1), independent `last_message` lifetime (Task 1), liveness and pid reuse (Task 2), silent/total hook (Task 3), tool with the three distinct answers and the 30-minute window (Task 4), perishable notices (Task 5), announcement wording, the `idle_ts >= since` guard and in-memory dedupe (Task 6), the installer's merge/backup/idempotence (Task 7), degradation probe (Task 8).
- **Builder decisions now settled by this plan:** the watcher is its own task; the installer is worth writing; `claude_status` takes no arguments.
- **Still open to the implementer:** exact summary phrasing, and how two concurrent sessions in one project are labelled (Task 4 only pins that both appear).
