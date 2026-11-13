# Claude Code status, through JARVIS

**Date:** 2026-11-12
**Author:** Akshit Kartik (@AKAAKS)
**Status:** implemented 2026-11-12 (see `docs/superpowers/plans/2026-11-12-claude-code-status.md`)

## Intent and why

The user starts a long Claude Code turn and walks away — kitchen, couch, another
room. Right now the only way to learn what happened is to come back and look at
the screen. Half the time the session is not working at all: it finished two
minutes ago, or it is sitting on a permission prompt waiting for a keystroke.

So: ask JARVIS, hands-free, "is Claude still going?" and get a true answer. And
when a session has been sitting unattended for a while, be told without having
to ask.

This is the first of two features brainstormed together. The second (a PC
remote: volume, media keys, display/power, browser tab close) is a separate
project and is not covered here.

## Goals

- Answer "is Claude still going?" for every live Claude Code session: **working**,
  **waiting on you**, **finished**, or nothing running.
- Include which project it is in, and how long it has been in that state.
- For a finished session, say what it actually said, not just that it stopped.
- Volunteer an announcement when a session has gone unattended ~a minute,
  through the proactive path fixed earlier today.
- Cost nothing per check: no screenshots, no vision, no extra API calls.

## Non-goals

- Any other app. Colab, Jupyter, a `python train.py` run — all deferred.
- Screen capture, OCR, vision of any kind.
- Controlling Claude Code. This is read-only: JARVIS reports, it never answers a
  prompt or approves a tool.
- Sessions that are not Claude Code CLI (claude.ai in a browser tab, for one).
- Tab-level or window-level detail about where the session is on screen.

## Anti-goals

These would make the feature a failure even if it technically worked:

- **Slowing down or breaking a Claude Code session.** The coding session is the
  work; this status feature is a nicety. A hook that raises, hangs, or writes
  noise to stdout is worse than no feature.
- **Confident wrong answers.** "Still working" about a session that died, or
  "waiting on you" about one that is not, sends the user back to the PC for
  nothing. "I can't tell" is a better answer than a wrong one.
- **Announcing while the user is sitting at the keyboard.** They can see the
  screen. An announcement then is noise, and noise is what gets a feature
  turned off.
- **Announcing something that is no longer true.** A notice banked overnight and
  delivered at 8am about a session that ended at midnight is worse than silence.

## Constraints

- Windows 11. The daemon runs under `pythonw` with no console. Claude Code
  2.1.323.
- JARVIS and Claude Code are separate processes. Several Claude sessions can run
  at once, in different working directories — or in the same one.
- Hooks are configured in `~/.claude/settings.json`. User-level hooks apply to
  every project and **merge** with project-level ones. The user's `hooks` block
  is currently empty, so there is nothing to collide with. That file is not in
  the repo, so the hook's absolute path never becomes public.
- The transcript (`transcript_path`) is written asynchronously and lags the
  current turn, and permission prompts never appear in it at all — it is UI
  state. So the transcript is not a usable source for this.
- `data/` is gitignored, so session state files never reach the public repo.
- A session's `last_assistant_message` is stored under `data/` and read back to
  the JARVIS brain when the user asks. That means a snippet of a Claude Code
  reply transits to the Anthropic API as tool output. It is the user's own
  content going to their own assistant, but it is a deliberate choice, hence
  truncation (see below).
- Repo conventions to follow: TDD; tool definitions + `handlers()` per module in
  `jarvisd/tools/`, wired in `jarvisd/tools/__init__.py:build_registry`; a tier
  in `security/policy.py`; and a `CAPABILITIES` entry in `jarvisd/core.py`
  (which is gated on registered tool names, so a missing entry means the model
  is never told the tool exists).

## Approaches

**1. Hooks write a per-session state file; JARVIS reads it. (chosen)**
Claude Code fires a hook on each lifecycle event; a small script records the
state. A new read-only tool summarizes the records. Event-driven, exact, free,
and it reuses a pattern already proven in this repo (`clients/activity.py`:
atomic write, cross-process, polled). It is also the only option that can see a
permission prompt at all.

**2. Tail the transcript, no hooks.** Zero setup, and it would work on sessions
already running. Rejected: the transcript lags the turn, permission state is
absent from it entirely — losing the most valuable answer — and the files are
12–50 MB.

**3. Hooks POST to the daemon over HTTP** (`http` hook type). Instant, no file
on disk. Rejected: jarvisd speaks only WebSocket, so this means standing up an
HTTP listener — new network surface for an identical result.

## The design

### States

Three live states, plus absence:

| State | Meaning |
|---|---|
| `working` | a turn is in flight |
| `waiting` | blocked on the user: a permission prompt |
| `done` | the turn finished; the session sits at an empty prompt |

`idle_prompt` is **not a state.** This is the correction to the first draft,
which mapped every notification to `waiting` and would therefore have announced
"Claude is waiting on you" about a session that had merely finished normally and
been left alone — both a wrong state and exactly the noise the third anti-goal
forbids. Idle is an *event about* the current state: it marks the record
announceable and leaves `state` alone. What gets said depends on what the state
already was:

- `waiting` + idle → "Claude needs your input in `<project>`."
- `done` + idle → "Claude finished in `<project>`" + what it said.
- `working` + idle → nothing. A long turn is not news.

### The hook script

One script, `scripts/claude_status_hook.py`, registered for six events in
`~/.claude/settings.json`. It reads the event JSON on stdin and updates one
record per session.

| Event | Effect on the record |
|---|---|
| `SessionStart` | create; `state = working` |
| `UserPromptSubmit` | `state = working`; clear `last_message` |
| `PermissionRequest` | `state = waiting`; set `tool_name` |
| `Notification` matcher `permission_prompt` | `state = waiting` |
| `Notification` matcher `idle_prompt` | `idle_ts = now`; **state untouched** |
| `Stop` | `state = done`; set `last_message` |
| `SessionEnd` | delete the record |

Properties it must have:

- **`async: true`** on every entry, so Claude Code never waits on it.
- **Silent and total.** Reads stdin, catches everything, exits 0 always. Nothing
  on stdout or stderr ever (stdout is interpreted by Claude Code on some
  events).
- **Field-name tolerant.** Every field read with `.get()`. The docs do not
  publish the exact payload for `Notification`'s message or `Stop`'s flags, so
  the script stores what it finds and never depends on a field being present.
- **Independent events.** Each event is handled on its own and a missing one
  degrades the feature rather than breaking it — see "Degradation" below.
- **Absolute paths.** The hook runs with the *Claude session's* cwd, which
  varies, so the state directory is derived from `__file__`, not from `.`.

### Storage

`data/claude_sessions/<session_id>.json`, one file per session, written
atomically (temp file + `os.replace`) exactly as `clients/activity.py` does it.

Record fields: `session_id`, `state`, `since` (epoch of the last *state*
change), `updated_ts`, `idle_ts`, `cwd`, `project` (basename of cwd),
`permission_mode`, `tool_name`, `last_message`, `claude_pid`, `claude_started`.

**`state` and `last_message` have independent lifetimes.** In the first draft a
single `state` field meant an idle notification 60s after `Stop` overwrote the
record and destroyed the finished session's message — defeating the goal of
saying what it actually said. `last_message` is written only by `Stop` and
cleared only by the next `UserPromptSubmit`.

**The hook is the only writer.** Nothing else touches the state directory. In
particular the daemon never writes to it: a cross-process read-modify-write
against the hook's atomic replace could revert a state the hook had just
written, which is precisely the "confident wrong answer" the anti-goals name.
Announcement dedupe therefore lives in daemon memory, not on disk (see below).

**Staleness is by process liveness, not by TTL.** A Claude session can sit
legitimately on a permission prompt for hours, so the TTL used for the tray
indicator would expire a true state.

`os.getppid()` is *not* the Claude process: on Windows the hook's parent is a
transient shell. Verified ancestry from a hook-like child on this machine:
`bash → bash → bash → claude.exe → powershell → WindowsTerminal`. So the hook
**walks the ancestor chain** (psutil, already a dependency) for a process named
`claude.exe` and records its pid *and* its `create_time()`. The reader treats a
record as ended when that pid is gone, or is alive but started at a different
time — which is what defeats pid reuse. If no `claude.exe` ancestor is found,
the record stores no pid and falls back to a 12-hour TTL, so a weird launch
shape degrades to "eventually forgotten" rather than "immediately invisible".

`last_message` is truncated — 500 characters proposed — both to keep the record
small and to bound what transits to the API.

### Degradation

The payload shapes for `Notification` and `PermissionRequest` are not published,
and a decision-point event may or may not run an `async` hook. So no single
event is load-bearing:

- With only `UserPromptSubmit` and `Stop`, the feature still answers
  working / finished / nothing running. That is a shippable v1.
- Permission detection (`PermissionRequest`, `Notification[permission_prompt]`)
  is the prize but not a gate. The build registers all of them and a probe
  confirms which actually fire; whatever does not fire simply never appears.

### The tool

`claude_status`, in a new `jarvisd/tools/dev_status.py`:

- Takes no arguments. Reads the state directory directly; no DB table, no new
  store class.
- `read_only` in `security/policy.py`.
- **Registered unconditionally**, like `terminal` and `clipboard`, so the
  `CAPABILITIES` line is always present.
- **"Not installed" is a distinct answer from "nothing running."** A missing
  state directory means the hooks were never wired, and saying "nothing is
  running" in that case is exactly the confident-wrong-answer failure. Missing
  directory says so; empty directory says nothing is running.
- Reports `done` sessions finished within the last 30 minutes with their
  message; older finished sessions are counted, not quoted. Otherwise someone
  with four terminals open all day gets four stale snippets on every check, and
  four times the reply text transits to the API.

### The announcement

Push fires on the `idle_ts` set by `idle_prompt` — Claude Code's own "idle ~60s"
signal, which means the debounce the anti-goal calls for comes for free: if the
user were at the keyboard, they would have answered and it would never fire.
Wording follows the state, per "States" above; `working` announces nothing.

The daemon polls the state directory, dedupes in memory on
`(session_id, state, since)`, and calls `notifier.notify(...)` — the proactive
path repaired earlier today, which reaches a connected voice client and falls
back to a local toast. A daemon restart can re-announce a session that is
*still* waiting, which is acceptable and arguably correct.

**These notices are perishable.** `notify()` today banks a notice during quiet
hours and `drain()` appends it to the next turn, so "Claude has been waiting on
you" could be delivered at 8am about a session that ended at midnight. This
needs a `perishable=True` option on `Notifier.notify` that skips the queue
entirely: if there is no route right now, drop it and log. That is a small
addition to `notifier.py` and the fourth anti-goal is why it exists.

## Left to the builder

- Exact wording of the spoken summaries.
- Whether the watcher is its own asyncio task or folded into `_reminder_tick`
  (which already runs every 15s).
- Whether `scripts/install_claude_hooks.py` is worth writing to merge the hook
  entries into `~/.claude/settings.json` idempotently, versus editing that file
  by hand once. If written, it must merge rather than overwrite, and back the
  file up first.
- How to name two concurrent sessions in the same project.
- Test strategy for the hook: feed it event JSON on stdin, assert the record on
  disk. No Claude Code process required.

## What actually fired (probe, 2026-11-12)

Run against the real `~/.claude/settings.json` with all six events registered
`async: true`, using headless `claude -p` sessions and polling
`data/claude_sessions/`:

| Event | Fires | Evidence |
|---|---|---|
| `SessionStart` | yes | a `working` record appears within ~1s of launch, with `cwd`, `project`, `permission_mode: "default"`, and the real `claude.exe` pid |
| `UserPromptSubmit` | yes | a second write follows ~0.5s later, same state, new `updated_ts` |
| `Stop` | yes | the record flips to `done` and carries `last_assistant_message` |
| `SessionEnd` | yes | the record file disappears on exit (confirmed by disabling the unlink: the record then survives) |
| `PermissionRequest` | **unconfirmed** | a headless session auto-approves, so no prompt can be raised from a script |
| `Notification` (idle / permission) | **unconfirmed** | a headless session never sits idle, and its payload shape is still unpublished |

Degradation is therefore as the design planned for: the **pull** path is fully
working on confirmed events (`working` / `done` + the message / nothing
running / hooks not installed were all observed live through the real
`claude_status` handler). The **push** path depends on `Notification`'s idle
signal, which only an interactive session can produce; if it turns out never to
fire, the announcement simply never happens and nothing else degrades.

Also worth recording: in a one-shot `claude -p` run, `Stop` and `SessionEnd`
land so close together that the `done` record is deleted before any poll sees
it. That is correct behaviour, not a bug -- an ended session should not be
reported -- but it means "no record" cannot be read as "Stop did not fire".

## Decisions taken during the build

- **The `Notification` matcher split is not implemented as matchers.** The
  design table above lists `Notification` twice, by matcher
  (`permission_prompt` / `idle_prompt`). The hook registers `Notification`
  once, with no matcher, and decides from the payload text
  (`_PERMISSION_HINTS` in `jarvisd/claude_sessions.py`); anything that does not
  look like a permission prompt falls through to idle, which is the quieter
  answer. The payload shape is unpublished, so sniffing degrades better than a
  matcher that may never match.
- **The announcement goes out at `urgent`, not `high`.** A perishable `high`
  notice is dropped during the default 23:00-08:00 quiet window, which would
  make the push half of this feature dark for nine hours a day -- and this user
  routinely works past 02:00 (the same reason timers were exempted). The idle
  signal already proves the user is at the PC but away from the keyboard, so
  the notice is either delivered now or forgotten, never banked.
- **`install_claude_hooks.py` creates `data/claude_sessions/`.** The tool reads
  a missing directory as "the hooks were never installed", which is deliberate
  -- but the directory was otherwise created by the first hook write, so
  installing and then asking before starting a session answered with
  instructions to install what had just been installed.
- **The announcement is dispatched through `asyncio.to_thread`.**
  `notify()` reaches `_push_from_thread`, which blocks its calling thread on a
  coroutine scheduled on the event loop; called on the loop thread it stalls
  for the full 10s timeout and reaches nobody. `_fire_due_reminders` already
  did this; the watcher now does too.

## Second review, and the state machine it corrected

A second review of the branch found three failures that all came from the same
gap: nothing reported the END of a wait, or the resumption of real work, in the
middle of a turn.

- A **second permission prompt in one turn was never announced.** `since` moves
  only on a real state change and the daemon dedupes on
  `(session_id, state, since)`, so prompt #2 reused prompt #1's key. Approve
  one, walk away, and the next one was silent forever -- the feature's headline
  case.
- After the user approved, the record still said **`waiting` while Claude
  worked**, so `claude_status` answered "needs your input, waiting 25 minutes"
  about a session that was busy.
- `SessionStart` set **`working` for a terminal nobody had typed into**, so
  three terminals left open answered "Claude is working, two hours so far".

Fixes:

- **`PostToolUse` and `PostToolUseFailure` are now registered** (eight events,
  not six). A finished tool is the only available proof that the prompt in
  front of it was answered. Both are needed: the published table says
  `PostToolUse` runs "after successful tool", and a denied prompt fires
  `PostToolUseFailure`. The cost is one hook process per tool call, which is
  the price of not reporting a stale state.
- **A fourth state, `idle`** -- the terminal is open and nothing has been asked
  of it. It reads as "Claude is open in X with nothing running" and announces
  nothing, because an untouched terminal is no more news than a long turn.
  Neither `working` nor `done` was true of it.
- **`notify(..., local_only=True)`** stops the fallback chain before the Home
  Assistant phone push. Ignoring quiet hours is only reasonable while the
  notice stays on this machine; "Claude finished in X" buzzing a phone at 4am
  is the notice that gets a feature switched off.
- The installer now **refuses a non-object `hooks` value** instead of silently
  replacing it, matching what it already did for a non-object top level.

Corrected from the first probe table: `PermissionRequest` **is** a real event
in Claude Code 2.1.323 -- its own hook-events table lists it ("Run before
permission prompt", matcher = tool name), alongside `PreToolUse`,
`PostToolUse`, `PostToolUseFailure`, `PostCompact` and the rest. Whether it
fires for an `async` hook is still unconfirmed.

Known and accepted: `announcement_for` still requires `idle_ts`, which only the
idle `Notification` sets, so the push path depends on Claude Code emitting that
signal while a permission dialog is open. Satisfying the guard from the
permission event instead would announce while the user is sitting at the
keyboard, which the third anti-goal forbids, so this stays as designed until
the idle signal is observed in an interactive session.

## `Stop` is one of three ways a turn ends

Found by running the finished feature against this machine rather than against
its tests. Asked in earnest, `claude_status` answered:

    Claude is working in claude-code-status (session 1), 2 minutes so far.
    Claude is working in claude-code-status (session 2), 35 minutes so far.
    Claude is working in claude-code-status (session 3), 35 minutes so far.
    Claude is working in claude-code-status (session 4), 35 minutes so far.
    Claude is working in claude-code-status (session 5), 36 minutes so far.
    Claude is working in akshi, 37 minutes so far.

One clause of six was true. Liveness was working correctly -- it had already
dropped two records whose processes had exited -- but a finished *subagent*
session is still a live process, and nothing retired it.

Claude Code 2.1.323's internal event list is longer than the documented table:

    PostToolBatch, Notification, UserPromptSubmit, UserPromptExpansion,
    SessionStart, SessionEnd, Stop, StopFailure, SubagentStart, SubagentStop,
    PreCompact, PostCompact, PreModelSwitch, PostModelSwitch,
    PermissionRequest, PermissionDenied, Setup, TeammateIdle, Task

So `Stop` covers only one of three endings. Now registered as well:

- **`SubagentStop`** -- a subagent or forked session's turn. This was the whole
  six-clause answer above: every agent dispatched during the build started a
  session that never reported finishing.
- **`StopFailure`** -- a turn that ended in an error, which otherwise claimed
  to be working for as long as its process lived.
- **`PermissionDenied`** -- a prompt the user refused, which ends the wait just
  as `PostToolUseFailure` does.

Eleven events in total. After the re-install the same question answered with
one clause: "Claude is working in claude-code-status, less than a minute so
far" -- the turn that asked it.

The lesson for the next reader: the tests and two code reviews all passed over
this, because every one of them reasoned from the event list the build started
with. Only running the thing found it.

## The idle signal does not arrive during a permission prompt

Measured on the merged code, with a real prompt left open:

    11c711c9  state=waiting  project=akshi  since=197s ago
              tool=AskUserQuestion  idle_ts=null

`PermissionRequest` **does** fire for an `async` hook -- previously unconfirmed
-- and it captures the tool name. But Claude Code sends no idle `Notification`
while the dialog is open, so `idle_ts` stayed null and `announcement_for`
returned None. Gated on idle, the headline case could never fire at all.

The same tick announced the `done` case correctly in the same minute:

    00:07:26 notify[urgent] -> local toast/chime: Claude finished in
             personal-assistant. It said: ...

So idle fires for a session sitting at an empty prompt, and not for one sitting
on a dialog.

**`WAIT_ANNOUNCE_SEC = 60`.** For `waiting`, the length of the wait is now the
debounce: a prompt open for a minute is announced whether or not an idle signal
ever arrives. This keeps the third anti-goal intact -- a prompt the user is
sitting in front of is answered well inside a minute, so an announcement still
only reaches someone who has walked away. A fresh idle signal still short-cuts
the wait if one ever does arrive, and `done` keeps the original idle gate,
which demonstrably works.

**`ANNOUNCE_MESSAGE_CAP = 120`.** The first production announcement recited
about 500 characters of a merge report into a toast. The record still keeps 500
for `claude_status` to read back on request; the spoken notice now carries
about a sentence, cut at a sentence boundary where there is one.
