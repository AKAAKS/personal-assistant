# JARVIS — personal assistant

Always-on brain daemon (`jarvisd`) + thin clients. Claude tool-use agent
controlling Home Assistant, Spotify, Android TV, and this PC, with tiered
Haiku/Sonnet/local routing, a $20/month budget guard, and a security layer.

Design docs: `docs/superpowers/specs/`.

## Highlights

- **Agentic tool-use loop** — Claude drives real devices and the OS through a
  registry of guarded tools (Home Assistant, Spotify, Android TV, file system,
  web search), not a fixed command set.
- **Tiered routing with a hard budget** — a regex intent classifier sends each
  turn to a local model, Haiku, or Sonnet; a cache-aware cost tracker enforces a
  `$20/month` cap and force-downgrades near the limit.
- **Security guard as a single choke point** — every tool call passes through
  permission tiers (read-only / write / destructive / forbidden), a path
  allowlist + secret-file block, blocked-URL/ID patterns, and an audit log.
  Destructive actions require a **turn-bounded** human confirmation, so the model
  can't self-confirm its own destructive call inside one reasoning loop (closed a
  live prompt-injection hole).
- **Proactive, not just reactive** — reminders that survive restarts (with a
  spoken → phone-push → "while you were away" fallback chain) and Home Assistant
  event-triggered automations, direct ($0) or AI-reasoned.
- **Memory** — SQLite FTS episodic recall + a curated profile injected into the
  prompt; conversation history restored across daemon restarts.
- **Nightly habit mining** — pure-Python pattern mining over the audit log turns
  recurring actions into one-line proposed facts (~1 Haiku call per new
  candidate, $0 otherwise); nothing is written without your approval.
- **Offline-first voice** — "hey jarvis" wake word, Whisper STT, and TTS that
  prefers offline SAPI5 but can use free neural voices, with automatic fallback.
- **Built test-first** — **517 passing tests**, a thin-client/daemon split
  designed for a future Raspberry Pi satellite, and loopback-only networking.

## Setup

    pip install -r requirements.txt
    copy .env.example .env    # then fill in keys

## Run

    python -m jarvisd                 # the brain (add --no-speech to skip Whisper/TTS)
    python -m clients.cli             # typed chat
    python -m clients.voice_client    # "hey jarvis" voice mode

The daemon binds `127.0.0.1:8765` (override with `JARVIS_HOST`/`JARVIS_PORT`).
Warning: the WebSocket has no authentication — do not set `JARVIS_HOST` to a
non-loopback address; that would expose full HA/PC/TV control to your network.

## Memory

JARVIS remembers you and your preferences:

- **Profile** (`data/profile.md`): a curated file of facts the assistant always knows. Edit it
  directly; changes take effect immediately on the next request (no restart needed).
  Use "Remember that ..." to have JARVIS add a fact.
- **Recall**: ask "what did we talk about volume?" to search past conversations.
- **History**: conversation history is persisted in SQLite and restored on daemon restart,
  so JARVIS knows the context of recent interactions even after it restarts.

Note: The design spec calls for `memory/profile.md`, but we use `data/` because it is
already gitignored and holds all personal state.

## Proactivity

JARVIS can act without being spoken to first: reminders that survive restarts, and
Home Assistant event triggers ("rules") that fire automations directly or hand them
to the AI.

**Reminders** — just ask, in plain language:

- "Remind me in 20 minutes to take the laundry out."
- "Remind me every day at 8am to take my meds." (daily/weekly recurrence)
- "What are my reminders?"
- "Cancel reminder 2."

A reminder fires by speaking through a connected voice/CLI client if one is open;
otherwise it falls back to a Home Assistant phone push, and if that also fails (or
HA is down) it's queued and delivered as "While you were away: ..." the next time
you chat.

**Trigger rules** — ask JARVIS to create an automation from an HA event. Two kinds:

- *Direct* (runs a tool call immediately, no AI involved, $0 cost):
  "If the hall motion sensor turns on after 11pm, turn on the hall light at 20%
  brightness."
- *Smart* (the instruction is handed to the AI to reason about when the event
  fires; cooldown-limited to avoid spam):
  "If the front door opens and nobody is home, check the camera and notify me if
  it's not one of us."

Rules require Home Assistant's WebSocket API (same `HA_URL`/`HA_TOKEN` as
everything else) and only take effect once HA is reachable. The daemon tolerates
HA being down or restarting at any time: the event listener reconnects with
backoff in the background, and reminders/the notifier keep working with or
without it (a phone push just fails over to the queue).

Env vars:

- `QUIET_HOURS` (default `23:00-08:00`) — during this window, proactive messages
  are always queued instead of spoken/pushed immediately (handles overnight
  wraparound; set to an empty string to disable quiet hours entirely).

## Location & weather

JARVIS knows where you are, so it never asks. It reads your location from the
**Windows Location API** (precise GPS) on a background thread, caches it, and
reverse-geocodes it to a city name. Turn it on once at **Settings → Privacy &
security → Location → "Let desktop apps access your location"**. If location is
off or unavailable, it falls back to a home location you set (`JARVIS_HOME_LAT`/
`LON`/`NAME` in `.env`).

Weather uses a dedicated `get_weather` tool backed by **Open-Meteo** (free, no API
key) — so "what's the weather?", "do I need a jacket?", and the briefing are instant
and **don't touch your web-search budget**. Ask about anywhere by name too ("weather
in Tokyo"). Units follow `JARVIS_UNITS` (`metric` default, or `imperial`).

## Web search & briefing

JARVIS can search the web (server-side, with citations) for anything that needs
current information — news, sports scores, prices. Ask directly, or ask for
everything at once:

- "Give me my briefing" — weather + top headlines (web search) + today's reminders
  + today's meals, in one reply.

Each search costs money (~$0.01/search, billed per-call on top of tokens), so it's
only used when the answer needs current information — not for things Claude already
knows. A hard monthly cap (`SEARCH_MONTHLY_CAP`, default 100) stops search from being
offered to the model at all once hit; the rest of JARVIS keeps working as normal.
Spend is tracked in `logs/cost.jsonl` alongside token costs.

## Files

JARVIS can search and read your files, and run scripts you've explicitly approved.
Three tools:

- **search_files** — find files by filename within allowed folders.
- **read_document** — read a document's text (.txt/.md/.pdf/.docx and other text
  files); long documents are summarized instead of dumped in full.
- **run_script** — run a script from a whitelisted folder.

All file access is restricted to allowed folders — by default `~/Documents`,
`~/Projects`, `~/Downloads` (override with `FILE_ROOTS`, semicolon-separated). Files
that look like secrets (`.env`, `*.key`, `*credential*`, etc.) are always blocked,
even inside an allowed folder.

`run_script` only runs scripts that already live in a whitelist folder (default
`scripts/jarvis_approved/`, override with `JARVIS_SCRIPTS_DIR`) — nothing outside it
can ever execute. Running a script is destructive, so it always asks for confirmation
first; say "yes, run it" and JARVIS retries the same call to actually run it. A
trigger rule (HA automation) can never get past this confirmation on its own — running
scripts autonomously, without a person present to confirm, is not allowed.

## Meal tracking

Log meals by voice or text and JARVIS estimates macros for trend tracking:

- "Log a meal: two eggs and toast with butter" — logs with estimated calories/
  protein/carbs/fat, a stated confidence, and any portion assumptions made.
- "How am I doing today?" — today's totals across everything logged.
- "Export my meal log" — writes a CSV or JSON file to `data/exports/`.

Important: these are **LLM estimates, not a food database lookup**. JARVIS states
its confidence (low/medium/high) and assumptions honestly with every log, but treat
the numbers as directionally useful for spotting trends — not as precise nutrition
facts.

## Habit suggestions

Every night (~3am local, override with `HABIT_SCAN_HOUR`) JARVIS mines
`logs/audit.jsonl` for recurring patterns — the same tool called with the same
input around the same time of day, several days running — and turns each new
one into a one-sentence proposed fact via a single Haiku call. Nothing is
written automatically and nothing interrupts you: the only thing you'll ever
see unprompted is a one-line trailer on a reply, like `💡 2 suggestions`, when
there's something pending.

- "Show suggestions" — lists the pending proposals.
- "Accept 1" — writes the fact to your profile (it survives a restart) and
  marks the proposal accepted.
- "Dismiss 2" — drops it; a dismissed or already-accepted habit is never
  re-proposed, and re-scanning an already-seen pattern costs nothing (no
  Haiku call — dedup happens before phrasing).

A day with no qualifying habits, or only below-threshold/single-day patterns,
spends $0. Mining itself is pure Python; the only cost is one Haiku call per
brand-new confirmed candidate.

## Voice

Default TTS is offline `pyttsx3` (Windows SAPI5). Set `TTS_BACKEND=edge` to
opt into `edge-tts`'s free Microsoft neural voices (`EDGE_TTS_VOICE`, default
`en-US-GuyNeural`) — higher quality, but it needs network access to Microsoft's
service. No `ffmpeg` install is required: the MP3 response is decoded to PCM
in-process via `miniaudio`. If the network is down or synthesis fails for any
reason, JARVIS automatically falls back to the offline `pyttsx3` voice rather
than freezing or dropping audio entirely.

Say "hey jarvis" while JARVIS is talking to interrupt it and give a new
instruction or correction.

## Always-on

    scripts\install_autostart.bat     # registers a logon task (does NOT start it)
    scripts\start_jarvisd.bat         # start it now; logs -> logs/jarvisd.log
    scripts\stop_jarvisd.bat          # stop it (ends the task AND frees the port)
    scripts\uninstall_autostart.bat

Only one daemon can hold the port: if `python -m jarvisd` reports "Cannot
bind", the scheduled task's instance is already running — stop it first.
Spotify auth opens a browser once; after that the refresh token is cached
in `data/spotify_tokens.json` and startup is silent.

## Tests

    pytest

Marker `slow` covers tests that load the real SAPI5 TTS engine:
`pytest -m "not slow"` skips them.

## Layout

    jarvisd/          brain: agent core, tool registry, WebSocket server, SQLite
    clients/          thin clients (CLI, voice)
    router/           intent classifier, cost tracker, Ollama backend
    security/         guard, policy, audit log
    voice/            STT/TTS (used by brain), mic/VAD/wake word (used by clients)
    *_client.py       device clients (Home Assistant, Spotify, Android TV, system)
