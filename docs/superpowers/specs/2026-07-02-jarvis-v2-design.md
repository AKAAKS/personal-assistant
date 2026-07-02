# JARVIS v2 — Always-On Personal Assistant: Design

**Date:** 2026-07-02
**Status:** Approved by user (conversation review, all sections)
**Project:** `C:\Users\akshi\Projects\personal-assistant`

## Goal

Evolve the existing JARVIS personal assistant from a launch-when-needed program into an **always-on brain service** with persistent memory, proactive behavior (reminders, Home Assistant event triggers, habit learning), and new capabilities (web search, file work, voice meal tracking) — while staying within a **$20/month API budget** and remaining architecturally ready to scale to Raspberry Pi voice satellites around the house.

Chosen approach: **evolve the current custom Python stack** (not Claude Agent SDK, not Home Assistant Assist). The existing tiered router, cost tracker, and security guard stay at the center; new subsystems are added around them.

## Decisions from brainstorming

| Question | Decision |
|---|---|
| JARVIS priority | Memory + proactivity, and breadth of capability (not latency/voice polish, not multi-room — yet) |
| New capabilities | Web search + on-demand briefings, deeper PC/file work, meal tracking |
| Meal data destination | JARVIS's own log with clean CSV/JSON export (MacroFactor has no public API) |
| Proactive behaviors | Reminders + nudges, HA event triggers, habit learning. No scheduled briefings — briefings on demand only |
| Runtime | 24/7 service on the PC (auto-start at login), architected as brain + thin clients for future Pi satellites |
| Phone (phase 1) | Push-only via Home Assistant companion app; two-way phone chat is future work |
| Budget | Keep $20/month, engineer around it |

## 1. Architecture & process model

Two kinds of processes:

**`jarvisd` — the brain (one instance, always on).** Auto-starts at login via Task Scheduler, runs hidden. (A true Windows service can't easily use CUDA + audio in the user session, so auto-start-at-login is the pragmatic "always on.") Owns all state:

```
jarvisd
├── WebSocket/HTTP API (localhost now, LAN later)   ← the satellite seam
├── Agent core        (today's Assistant, refactored)
│   ├── tools/        ha, spotify, tv, system  +new: web, files, meals, reminders, memory
│   ├── router/       classifier, cost tracker, ollama backend
│   └── security/     guard + audit (extended for new tools)
├── STT (faster-whisper, CUDA) + TTS                 ← centralized so clients stay thin
├── Memory store      (SQLite + profile.md)          [section 2]
└── Proactivity engine (scheduler, HA events, habit miner) [section 3]
```

**Clients — as many as needed, all dumb.** A client does three things: detect the wake word locally, stream audio (or text) to the brain, play back what the brain sends. Phase 1 ships two: the PC voice client (today's `voice/` code minus STT/TTS) and a CLI for typed chat. A future Pi satellite is the same client code on different hardware.

**Protocol:** WebSocket carrying JSON events (`text_in`, `reply`, `proactive_say`) plus binary audio frames. The brain can push to any connected client — that's how proactive speech reaches the room you're in. If no client is connected, proactive output falls back to HA phone push.

**Placement decisions:**
- STT/TTS live in the brain (Whisper needs the GPU; makes satellites cheap — a Pi only needs openwakeword + mic).
- Brain and voice client are separate processes from day one, even on the same PC, so the satellite seam exists and is proven.

## 2. Memory

Three layers, no vector database (keyword search + a curated profile beats embeddings at single-user scale; the `recall` tool's interface hides the internals so this can change later):

1. **Profile — always known.** `memory/profile.md`, human-readable, curated facts (preferences, device nicknames, standing instructions). Injected into the system prompt on every call; prompt-cached so it costs ~nothing. Capped ~1,500 tokens. Grows via a `remember` tool; user can edit the file directly.
2. **Episodic — what happened.** Conversations, meals, reminders, events in one SQLite database with FTS5 full-text search. A `recall` tool lets the agent query it on demand — free local queries instead of context stuffing. Conversation history reloads on restart.
3. **Derived — what's usually true.** Patterns mined nightly from layer 2 by the habit miner (section 3), proposed for promotion into the profile.

## 3. Proactivity engine

Three input sources, one output path:

1. **Reminders & nudges.** APScheduler backed by SQLite (survives restarts). Tools: `set_reminder`, `list_reminders`, `cancel_reminder`; natural time parsing by the agent. Standing nudges = recurring reminders with an optional local condition check (e.g. "8:30pm nudge to log dinner *unless* a meal was logged").
2. **HA event triggers.** Persistent WebSocket subscription to Home Assistant's event bus. Rules stored in SQLite, created conversationally. Two kinds: **direct rules** (event → device action, pure Python, $0) and **smart rules** (wake the agent with event context; rate-limited so a flapping sensor can't burn budget).
3. **Habit learning.** Nightly local job: plain frequency analysis over audit/event logs (same action, same time window, ≥N times in M days). Only a confirmed candidate spends one Haiku call to phrase a proposal. Proposals never interrupt — they queue and surface at the next interaction. Accepting one writes a direct rule or profile entry.

**Notifier (single output path).** Every proactive message carries a priority: high → spoken on a connected client, falling back to HA phone push; low → queued for next conversation. Quiet hours respected. All proactive actions pass through the security guard and audit log like user-initiated ones.

Idle-day API cost of the whole engine: near zero.

## 4. New capabilities

**Web search & on-demand briefings.** Anthropic's server-side `web_search` tool (no keys, citations included; use the newest tool variant the chosen model supports). Search is priced per use on top of tokens — pin the current rate at implementation time, meter it in the cost tracker, and enforce a hard monthly search cap. "Give me my briefing" is an ordinary agent request (weather + headlines + today's meals/reminders), on demand only.

**Deeper PC & file work.** Three tools, deliberately scoped:
- `search_files` — Everything CLI (`es.exe`) if installed, directory-walk fallback. Allowlisted roots only (Documents, Projects, Downloads).
- `read_document` — extract text (txt/md/pdf/docx) + summarize via Haiku/Ollama, with a size cap.
- `run_script` — executes only scripts from a user-maintained whitelist folder. Marked **destructive** → first real use of the guard's confirm-before-execute path.

Guard additions: path-allowlist enforcement, hard block on secret-shaped files (`.env`, key files) everywhere.

**Meal tracking.** `log_meal` tool: LLM estimates calories/protein/carbs/fat from the description with stated confidence and portion assumptions; writes to SQLite `meals` table. `meal_summary` answers "how am I doing today" as a pure local query. Evening nudge checks this table. One export command produces clean CSV/JSON (date, food, quantity, macros) portable to MacroFactor or anything else. Estimates are LLM guesses, not a food database — good enough for trends, honest about it.

## 5. Budget engineering ($20/month)

- **Models:** Haiku 4.5 (`claude-haiku-4-5`, $1/$5 per MTok) stays default; Sonnet tier upgrades `claude-sonnet-4-6` → `claude-sonnet-5` ($3/$15; intro $2/$10 through 2026-08-31). Sonnet 5 notes: rejects non-default sampling params (we send none); new tokenizer counts ~30% more tokens for the same text, shifting the cost baseline. Update `MODEL_PRICING`.
- **Cost tracker upgrades:** cache-aware accounting (cache reads ~0.1×, cache writes 1.25× — today's tracker over-reports by pricing all input at full rate); per-entry source tag (`interactive` / `proactive` / `habit` / `search`); metered web-search counter with hard monthly cap; monthly cost report.
- **Cache discipline:** cached prefix = tools → system prompt → profile (profile last, so profile edits invalidate only themselves). Haiku 4.5's minimum cacheable prefix is 4,096 tokens — the tool set clears it; keep the tool list stable and deterministically ordered (no conditional tool sets).
- **Free paths stay free:** wake word/VAD/Whisper/TTS local; greetings via canned replies or Ollama; direct rules, scheduler checks, meal summaries, reminder firing in pure Python. Budget downgrade (Sonnet→Haiku at 95%) unchanged.

## 6. Hygiene, security, testing

- `git init` + README (this commit starts the repo). `logs/` and `.env` gitignored.
- Split `assistant.py` (~770 lines) into `tools/` modules — one per domain, each exporting tool definitions + handlers into a registry.
- pytest suite for pure-logic parts: guard, classifier, cost tracker, meal parsing, memory store; clients mocked.
- Proactive actions audited identically to user-initiated ones.

## Phasing

Each phase independently useful; order fixed:

1. **Foundation** — brain/client split (WebSocket API, thin PC voice client, CLI), SQLite, auto-start at login, model refresh, git + tests + `tools/` refactor. *Exit: feature parity with today, but always-on and satellite-ready.*
2. **Memory** — profile + `remember`/`recall`, persistent history.
3. **Proactivity core** — reminders/nudges, Notifier (priorities + quiet hours), HA event triggers (direct + smart rules).
4. **Capabilities** — web search + briefing, file tools, meal tracking + export, `run_script` + guard extensions.
5. **Habit miner + polish** — nightly pattern job with proposal queue; optional free neural voice (edge-tts) to replace pyttsx3.

**Designed-for but not built now:** Pi/satellite voice clients, two-way phone chat (e.g. Telegram), lift-and-shift of the brain to a home server.

## Error handling principles

- Brain must survive client disconnects, HA outages (reconnect with backoff), Ollama absence (escalate to Haiku), and API failures (surface the error, never crash the daemon).
- Scheduler and trigger rules persist in SQLite; on restart the brain resumes them.
- All new tools return errors as strings to the agent (existing pattern) so the model can adapt.

## Testing strategy

- Unit tests (pytest) for guard, classifier, cost tracker, memory, meal parsing — no network.
- The WebSocket protocol gets a loopback integration test (fake client ↔ brain with mocked Anthropic).
- Live smoke test per phase: run the daemon, exercise one real flow end-to-end (documented manual checklist until satellites justify more).
