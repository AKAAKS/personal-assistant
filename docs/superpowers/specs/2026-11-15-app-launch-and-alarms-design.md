# App Launching & Alarm-Grade Timers — Design

**Date:** 2026-11-15
**Status:** approved (scope confirmed by user: full Google Home parity, unified app registry)

Two independent subsystems, diagnosed from evidence in `logs/audit.jsonl`,
`logs/jarvisd.log`, `logs/voice_client.log`, and `data/jarvis.db`.

---

## Part A — App launching

### Observed failure

Asking JARVIS to open the Windows Camera fails, re-asks for confirmation on
every attempt, reports the app is not open after it opened, and cannot close it.

### Evidence

```
launch_app {"target":"camera"}  -> "Could not find 'camera' on this PC"
run_command {"command":"explorer.exe shell:appsFolder\\Microsoft.WindowsCamera_8wekyb3d8bbwe!App"}
                                -> verdict "confirm_required"   (every single time)
                                -> "Exit code: 1"  stdout empty
remember  {"fact":"The Windows camera app can be launched using the command: explorer.exe shell:appsFolder\\..."}
close_app  {"name":"camera"}    -> "camera is not running."
```

### Root cause

`system_client.py` holds three hand-written, independently maintained dicts:

| dict | used by | covers |
|---|---|---|
| `app_map` | `launch_app` | classic `.exe` / a few protocols |
| `process_map` | `close_app` | classic `.exe` image names |
| `_RUNNING_NAMES` | `_is_running` | 12 apps only |

No UWP/AppX app appears in any of them, and nothing keeps them consistent. One
missing entry therefore produces four distinct user-visible failures:

1. `launch_app` cannot resolve `camera` -> honest failure.
2. The model routes around the failure into `run_command`, which is tier
   `destructive` in `security/policy.py` -> `confirm_required` **by correct
   design, forever**. The remembered fact permanently pins Camera to this path.
3. The fallback command returns exit 1, so the model truthfully reports failure.
4. `close_app` derives `camera.exe`; the real image is `WindowsCamera.exe`.

**Aggravating defect:** `list_processes` advertises "top 30 running processes
with memory usage" but computes `sorted(seen.items(), key=lambda x: x[0].lower())[:30]`
— the first 30 names **alphabetically**. On a ~200-process machine it returns
only A–C names, so `WindowsCamera.exe` can never appear and verification is
structurally impossible. `launch_app` also returns `"Launched {target}"` without
ever checking that anything started.

### Required behaviour

- One record per app: aliases, launch target, process image name. Launch, close,
  and is-running read the same record, so they cannot disagree.
- UWP apps reachable through `launch_app` (tier `write`, never confirms):
  camera, photos, mail, calendar app, clock/alarms, store, maps, settings,
  calculator, phone link, xbox, sound recorder, snipping tool, terminal.
- `launch_app` verifies after launching when the record names a process, and
  reports what is actually true.
- `close_app` resolves the same record, so closing works for UWP apps.
- `list_processes` returns the genuine top 30 by memory, matching its description.

---

## Part B — Timers and alarms

### Observed failure

A 20-minute timer did not alert, and JARVIS could not report timer state.

### Evidence

Reminder row 14: created `2026-11-15T05:33:38`, due `05:53:38`, `kind='timer'`,
`status='done'`. It fired on schedule:

```
05:53:54 [jarvisd.server]   proactive push: 1/1 client(s) reached (audio=True)
05:53:54 [jarvisd.notifier] notify[urgent] -> spoken to 1 client(s): Reminder: Timer's up.
05:53:54 [voice_client]     Playing proactive audio (111788 bytes)
```

At `05:56:23` the user asked "Do I have a timer on right now and is it done?" —
the log shows one API call and **no tool call**; the model answered from memory.

### Root cause B1 — delivery is first-route-wins

`Notifier.notify` returns as soon as `push_to_clients` reports >0 clients, so
`local_notify` (Windows toast + `winsound` chime) never runs. The user received
one 2.3-second spoken line, no chime, no toast, once, with no repeat and nothing
persisting in the Action Center. The chain is built to avoid double-notifying,
which is correct for a low-stakes nudge and wrong for an alarm the user is
actively waiting on.

### Root cause B2 — timer state is unqueryable

`ReminderStore.list_active` filters `status='active'`, so a timer becomes
invisible the instant it fires. It prints absolute ISO timestamps with no
remaining-time arithmetic, and `set_timer` returns no id.

### Confirmed gaps vs. a Google Home

Verified absent by grep across `jarvisd/`, `security/`, `router/`: alarm concept,
snooze, stopwatch, pause/resume, extend, cancel-by-label, ring-until-dismissed.
`recurrence` accepts only `none|daily|weekly` — no weekdays, weekends, specific
days-of-week, or monthly.

### Required behaviour

**Alarm-grade delivery.** A notice may be marked `alarm`. An alarm fires every
available channel at once (spoken + toast + chime) rather than the first that
works, and re-fires on an interval until the user dismisses it. A non-alarm
notice keeps today's exact first-route-wins behaviour.

**Queryable state.** A timer tool reports, for every live timer, its label, its
remaining time in spoken-friendly form, and whether it is running, paused, or
ringing; and reports timers that fired recently so "did my timer go off?" is
answerable.

**Controls.** Named and multiple concurrent timers; pause; resume; extend by N
minutes; snooze a ringing alarm; dismiss/stop; cancel by label as well as by id.

**Recurrence.** `none|daily|weekdays|weekends|weekly|monthly`, plus specific
days-of-week.

### Non-goals

- Stopwatch is in scope (user chose full parity) but is a separate state model
  from countdown timers and must not share the reminders table.
- Location-based reminders ("when I get home") — out of scope.
