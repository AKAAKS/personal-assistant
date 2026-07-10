# JARVIS v2 — Phase 5: Habit miner + neural voice (edge-tts)

Final phase. Spec: `docs/superpowers/specs/2026-07-02-jarvis-v2-design.md` §2.3 (derived memory), §3.3 (habit learning), §Phasing.5. Base branch: `master` @ 65edfb4→818a042 (Phase 4 merged, 399 tests). Work on branch `phase5-habits-voice`.

**Goal.** (1) A nightly, pure-Python **habit miner** that finds repeated actions (same tool, same time-of-day window, ≥N times over M days) in `logs/audit.jsonl`, spends at most one Haiku call per *confirmed* candidate to phrase a natural proposal, queues proposals (never interrupts), and — when the user accepts — writes a **direct trigger rule** or a **profile entry**. (2) An optional free **edge-tts** neural voice backend replacing pyttsx3, behind an env toggle with graceful fallback.

**Hard constraints carried from prior phases (do not violate):**
- Every proactive/miner-initiated tool action goes through `Assistant.execute_guarded` — the single guard+audit choke point. Proposals themselves are data, but *accepting* one calls `rules_store.add`/`profile_store.append` directly from a tool handler (write-tier), not a destructive tool.
- The event loop never calls a blocking store/miner/TTS method directly — always `await asyncio.to_thread(...)`. `_push_from_thread` deadlocks if run on the loop thread.
- Never open a text file without `encoding="utf-8"`.
- Handlers never raise — return `"Error: ..."` strings.
- Never commit `.env`, `logs/`, `data/`.
- The miner reads `logs/audit.jsonl` **read-only**; it must tolerate a missing, empty, partial, or malformed-line file (skip bad lines) and produce zero candidates rather than crash.
- Cost of the whole engine on an idle day ≈ $0: mining is pure Python; only a *confirmed* candidate (passed the frequency threshold AND not already proposed/accepted/dismissed) spends one Haiku call. Record every such call via `cost_tracker.record(..., source="habit")`.
- No new conditional tool-set churn that breaks the cache prefix: proposal tools register whenever the proposal store is present (deterministic), appended after existing tools, same ordering discipline as Phase 4.

---

## Task 1: schema v5 + HabitProposalStore

**Files:** Modify `jarvisd/db.py` (SCHEMA_VERSION 4→5, `MIGRATIONS[5]`). Create `jarvisd/proposal_store.py`. Test `tests/test_proposal_store.py` (new), extend `tests/test_db.py`.

**Interfaces:**
- `MIGRATIONS[5]`: `CREATE TABLE habit_proposals (id INTEGER PRIMARY KEY AUTOINCREMENT, created_ts TEXT NOT NULL, signature TEXT NOT NULL UNIQUE, kind TEXT NOT NULL, payload TEXT NOT NULL, proposal_text TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending')`.
  - `signature` = a stable dedupe key for the mined pattern (see Task 2) so the same habit is never re-proposed while a prior proposal is still pending/accepted/dismissed. `UNIQUE` enforces it at the DB layer.
  - `kind` ∈ {`"rule"`, `"profile"`} — what accepting it creates.
  - `payload` = JSON: for `rule`, the exact kwargs for `rules_store.add(...)`; for `profile`, `{"fact": "..."}`.
  - `status` ∈ {`pending`, `accepted`, `dismissed`}.
- `ProposalStore(conn)`:
  - `add(signature, kind, payload: dict, proposal_text, now=None) -> int | None` — INSERT; on `sqlite3.IntegrityError` (duplicate signature) return `None` (already proposed — caller skips). JSON-encodes payload.
  - `list_pending() -> list[Row]` (ordered by id).
  - `get(pid) -> Row | None`.
  - `mark_accepted(pid) -> bool`, `mark_dismissed(pid) -> bool` (guard: only transition from `pending`; return False otherwise).
  - `already_proposed(signature) -> bool` — True if a row with that signature exists in ANY status (pending, accepted, OR dismissed). REVISED DECISION (found during Task 3 integration): the `signature` column is UNIQUE, so a habit can be proposed at most once ever; a dismissed habit therefore stays suppressed (JARVIS does not re-nag or re-spend a Haiku call on a suggestion the user already declined). The earlier "dismissed resurfaces" idea both contradicted the UNIQUE constraint (add would silently drop the re-insert) and would have nagged nightly — dropped. Time-delayed resurfacing, if ever wanted, is a future feature.

- [ ] Step 1: Failing tests — v4→v5 upgrade preserves existing data + adds table (hand-built v4 DB via raw sqlite3 + MIGRATIONS through 4, seed a reminder+meal, reconnect, assert intact + habit_proposals exists + version 5); add/list_pending/get/mark_* transitions; duplicate signature returns None; already_proposed semantics (any status — pending, accepted, AND dismissed all return True).
- [ ] Step 2: RED. Step 3: implement. Step 4: GREEN + full suite.
- [ ] Step 5: Commit `feat: schema v5 habit_proposals + ProposalStore`

---

## Task 2: habit miner core (pure Python, no API)

**Files:** Create `jarvisd/habit_miner.py`. Test `tests/test_habit_miner.py` (new).

**Interfaces:**
- Consumes: path to `logs/audit.jsonl` (injectable for tests). Produces structured candidates; NO network, NO API, NO store writes.
- `mine_candidates(audit_path, now=None, min_occurrences=3, window_days=14, hour_tolerance=1) -> list[Candidate]` where `Candidate` is a dataclass: `signature: str`, `kind: str` ("rule"|"profile"), `payload: dict`, `evidence: dict` (counts, hour, tool, sample input — for the phrasing step).
  - Read the file line-by-line, `encoding="utf-8"`; `json.loads` each line inside try/except → skip malformed. Missing file → `[]`.
  - Keep only entries with `verdict == "allowed"` and a `ts` within the last `window_days`.
  - Group by a **pattern key**: `(tool, canonical_input_signature, hour_bucket)`. `hour_bucket` = local hour of `ts` (parse ISO, treat naive/UTC consistently — convert to local for time-of-day). `canonical_input_signature` = a deterministic reduction of `input` that captures the meaningful, repeatable part while ignoring volatile fields (e.g. for `control_light` use `{entity/room, action}`; for `spotify_play` the query/uri; drop free-text like transcripts). Implement a small per-tool projection map `INPUT_PROJECTION: dict[tool, list[keys]]`; tools not in the map are ignored (only *actionable, repeatable* tools mine into rules). Keep it conservative — better to miss a habit than propose noise.
  - A group with `count >= min_occurrences` across `>= min_occurrences` DISTINCT days (not 3 hits in one afternoon) → a candidate.
  - `kind`/`payload`: map the candidate to a **direct trigger rule** payload when the tool is a device action tied to a time (this is a *time* pattern, not an HA event — Phase 3 rules are event-triggered; a purely time-based habit maps better to a **reminder/profile note** than an HA event rule). DECISION: Phase 5 mines TIME-OF-DAY habits → `kind="profile"` with a fact like "Usually <action> around <hour>" (payload `{"fact": ...}`), because there is no time-based trigger-rule engine (rules are HA-event driven). Reserve `kind="rule"` for a future event-correlation miner. So **Task 2 emits only `kind="profile"` candidates**; the schema/tooling still supports `"rule"` for forward-compat. (This keeps Phase 5 honest: we propose profile knowledge, not fabricate an unsupported time-scheduler.)
  - `signature` = stable hash of `(tool, canonical_input_signature, hour_bucket)` — same input always yields same signature (dedupe key for Task 1).
- Determinism: same audit file + same `now` → identical candidate list (sort by signature). No wall-clock reads except via injected `now`.

- [ ] Step 1: Failing tests — build a synthetic audit.jsonl in tmp: a habit that fires 4 times across 4 days at ~19:00 → one candidate with the right signature/hour/fact; below-threshold (2×) → none; 3 hits same day → none (distinct-day rule); malformed line skipped; blocked-verdict entries ignored; missing file → []; volatile input fields don't split a group.
- [ ] Step 2: RED. Step 3: implement. Step 4: GREEN + full suite.
- [ ] Step 5: Commit `feat: pure-Python habit miner over audit log`

---

## Task 3: proposal phrasing + persistence pipeline

**Files:** Create `jarvisd/habit_pipeline.py` (or a function in `habit_miner.py`). Test `tests/test_habit_pipeline.py` (new).

**Interfaces:**
- `run_habit_scan(audit_path, proposal_store, phraser, now=None, **mine_kwargs) -> int` — orchestrates: `mine_candidates(...)` → for each candidate, skip if `proposal_store.already_proposed(signature)`; else call `phraser(candidate)` (ONE Haiku call, returns natural-language proposal text) and `proposal_store.add(signature, kind, payload, text, now)`. Returns count of NEW proposals stored. Never raises: a phraser exception on one candidate is logged and skipped (others still processed).
- `phraser` is injected (bootstrap supplies a closure that calls Haiku via the Assistant and records `source="habit"`). In tests, a spy phraser returns canned text and counts calls — proving: (a) at most one call per NEW candidate, (b) ZERO calls when `already_proposed` already blocks, (c) zero calls when there are no candidates.
- Cost/prompt: phraser prompt is small ("Given this recurring pattern <evidence>, write a one-sentence suggestion I can accept or dismiss, e.g. 'You often dim the living room around 7pm — want me to remember that?'"). `max_tokens` ~120.

- [ ] Step 1: Failing tests — full scan with a spy store + spy phraser: N candidates → N adds + N phraser calls; re-run with same audit (already_proposed now True) → 0 adds + 0 phraser calls (idempotent, no wasted API); phraser raising on one candidate doesn't abort the rest.
- [ ] Step 2: RED. Step 3: implement. Step 4: GREEN + full suite.
- [ ] Step 5: Commit `feat: habit scan pipeline (mine -> phrase once -> queue)`

---

## Task 4: proposal tools + accept/dismiss

**Files:** Create `jarvisd/tools/proposals.py`. Modify `jarvisd/tools/__init__.py`, `security/policy.py`. Test `tests/test_tools_proposals.py` (new), extend `tests/test_tools_registry.py`.

**Interfaces:**
- `proposals.TOOLS` + `proposals.handlers(proposal_store=None, rules_store=None, profile_store=None)`:
  - `list_proposals()` → pending proposals as an enumerated list "1) <text> [#id]" or "No pending suggestions." (read_only)
  - `accept_proposal(id)` → load by id; if not pending → "Error: no pending suggestion with id N." Dispatch by `kind`: `profile` → `profile_store.append(payload["fact"])`; `rule` → `rules_store.add(**payload)`. On success `mark_accepted`, return a confirmation. Any missing store → "Error: <thing> not available." (write)
  - `dismiss_proposal(id)` → `mark_dismissed`; friendly confirm or error. (write)
- Policy: `list_proposals` read_only, `accept_proposal`/`dismiss_proposal` write. (NOT destructive — writing a profile note or a $0 direct rule is reversible and low-risk; consistent with `remember`/`set_reminder` being write-tier.)
- Registry: register these when `proposal_store` is passed; params appended last.
- SYSTEM_PROMPT: add a short Capabilities line — "Habit suggestions: when the user asks about suggestions, or to surface them, use list_proposals; accept_proposal/dismiss_proposal act on them."

- [ ] Step 1: Failing tests — accept a `profile` proposal writes via a spy profile_store + marks accepted + second accept of same id errors (not pending); accept a `rule` proposal calls rules_store.add with the stored kwargs; dismiss marks dismissed; list formats/empty; missing-store error strings; unknown id error. Handlers never raise on bad input types.
- [ ] Step 2: RED. Step 3: implement. Step 4: GREEN + full suite.
- [ ] Step 5: Commit `feat: list/accept/dismiss habit-proposal tools`

---

## Task 5: nightly scan scheduler + surfacing

**Files:** Modify `jarvisd/server.py` (nightly async job + surface pending count on next interaction). Test `tests/test_server_habits.py` (new).

**Interfaces:**
- `JarvisServer.__init__(..., habit_scan=None)` appended last — `habit_scan` is a zero-arg callable (bootstrap closure) that runs `run_habit_scan(...)` and returns the count of new proposals; None disables the job.
- `_habit_tick()`: async loop mirroring `_reminder_tick` structure. Sleep until the next scan time (once per day; env `HABIT_SCAN_HOUR` default 3 = 3am local, low-traffic). Run `await asyncio.to_thread(self.habit_scan)` inside try/except → log count, never crash the daemon. Started in `serve_forever()` alongside `_reminder_task` only when `habit_scan is not None`.
- Surfacing (never interrupt): extend `_append_drained_notices` (or add a peer) so that when there are pending proposals, the reply gains a gentle trailer like "💡 You have N habit suggestions — say 'show suggestions' to review." Compute via `await asyncio.to_thread(proposal_store.list_pending)` (server needs a `proposal_store` ref too — add `proposal_store=None` param). Only append when count > 0; keep it to one line; do NOT dump all proposal text (the user pulls them via list_proposals). Guard against doubling with the existing "While you were away" notices.

- [ ] Step 1: Failing tests — with a fake habit_scan (returns 2) the tick calls it and survives an exception; the surfacing trailer appears when list_pending is non-empty and is absent when empty; the trailer doesn't clobber drained notices (both can appear). Use a short/monkeypatched interval so the test doesn't wait for 3am (inject next-run computation or patch the sleep).
- [ ] Step 2: RED. Step 3: implement. Step 4: GREEN + full suite.
- [ ] Step 5: Commit `feat: nightly habit scan + non-intrusive proposal surfacing`

---

## Task 6: edge-tts neural voice backend

**Files:** Create `voice/tts_edge.py`. Modify `voice/tts.py` (or a small factory), `requirements.txt` (`edge-tts`). Test `tests/test_tts_edge.py` (new).

**Interfaces:**
- Keep the existing `TextToSpeech.synthesize(text, timeout) -> bytes | None` contract (WAV bytes or None → caller degrades to text). Add `EdgeTTS` with the SAME method signature so `__main__`/server treat them interchangeably.
- `EdgeTTS(voice=os.getenv("EDGE_TTS_VOICE", "en-US-GuyNeural"), rate=None)`: uses the `edge-tts` package (async, network to Microsoft's free endpoint — no API key, $0). `synthesize` drives the async call from a worker thread with its own event loop (mirror the thread-isolation discipline in `tts.py`); enforce the same `SYNTH_TIMEOUT_SEC`/timeout join; on ANY failure (network down, package missing, timeout) return None so the caller degrades — and, when constructed via the factory with a pyttsx3 fallback available, the factory wraps edge with a fallback to pyttsx3.
  - Audio format: edge-tts yields MP3. The client plays WAV (server protocol is WAV-out). Convert MP3→WAV **without ffmpeg if possible**: prefer `audioread`/`soundfile` are heavy; DECISION: use `edge-tts` to write MP3 to a temp file, then decode with the stdlib-friendly path — if a pure-Python/bundled decoder isn't available, document that edge mode requires `ffmpeg` on PATH and fall back to pyttsx3 when conversion fails. The implementer must VERIFY on this machine whether MP3→WAV works out of the box; if it needs ffmpeg and ffmpeg is absent, that is a REPORTED BLOCKER for edge audio (the toggle still ships, defaulting off, with an honest README note) — do NOT fake success.
- Factory `make_tts(backend=os.getenv("TTS_BACKEND", "pyttsx3"), ...)` in `voice/tts.py`: `"edge"` → `EdgeTTS` wrapped with pyttsx3 fallback; anything else → `TextToSpeech`. Default stays pyttsx3 (safe, offline, known-good).

- [ ] Step 1: Failing tests — `make_tts("pyttsx3")` returns a TextToSpeech; `make_tts("edge")` returns something with a `.synthesize` (mock the edge-tts network call — do NOT hit the network in CI); edge synthesize returning None on a simulated failure; empty text → None. Mark any test that actually calls the edge endpoint `@pytest.mark.slow` and network-guard it (skip if offline). Verify MP3→WAV conversion path with a tiny fixture IF a decoder is available, else assert the documented fallback.
- [ ] Step 2: RED. Step 3: implement + `pip install edge-tts`. Step 4: GREEN + full suite. Report the ffmpeg/decoder finding explicitly.
- [ ] Step 5: Commit `feat: optional edge-tts neural voice backend with pyttsx3 fallback`

---

## Task 7: wiring + README + .env.example + ledger

**Files:** Modify `jarvisd/bootstrap.py`, `jarvisd/__main__.py`, `README.md`, `.env.example`. Test: bootstrap import smoke + full suite incl. slow.

**Interfaces:**
- `bootstrap.create_assistant()`: build `ProposalStore(conn)` in the same all-or-nothing store try/except (conn not None ⇒ proposal_store set). Build the `phraser` closure (Haiku via `ai.client`, records `source="habit"`) and the `habit_scan` closure (`run_habit_scan(AUDIT_PATH, proposal_store, phraser, ...)`). Return tuple extended with `proposal_store` + `habit_scan` (appended last). NOTE the tuple has grown each phase — update the single runtime caller (`__main__.run`) and `tests/test_bootstrap.py` unpack.
- `__main__.run`: unpack new returns; pass `proposal_store` + `habit_scan` into `JarvisServer`; select TTS via `make_tts(os.getenv("TTS_BACKEND","pyttsx3"), rate=...)` instead of the direct `TextToSpeech(...)`. Features line: append `"habits"` and the active TTS backend.
- `.env.example`: commented `HABIT_SCAN_HOUR=3`, `TTS_BACKEND=pyttsx3` (with `# or: edge`), `EDGE_TTS_VOICE=en-US-GuyNeural`. Placeholders only.
- README: **Habit suggestions** section (nightly, on-your-terms, "show suggestions" / accept / dismiss, what accepting does, that it's ~$0 and never interrupts) and a **Voice** note (edge-tts opt-in, needs network, falls back to pyttsx3, ffmpeg caveat if the implementer confirmed one).
- Ledger: append the Phase 5 section to `.superpowers/sdd/progress.md`.

- [ ] Step 1: Wire. Step 2: `python -c "from jarvisd.bootstrap import create_assistant; import jarvisd.__main__; print('OK')"`. Step 3: README + .env.example. Step 4: full suite incl. slow green.
- [ ] Step 5: Commit `feat: wire habit miner + edge-tts into daemon + README`

---

## Final acceptance checklist (manual, after merge)
1. `pytest` all green; daemon restart clean; `HABIT_SCAN_HOUR` respected.
2. Seed `logs/audit.jsonl` with a repeated action across several days → run the scan (or set HABIT_SCAN_HOUR to the current hour) → a pending proposal appears; next interaction shows the one-line "💡 N suggestions" trailer.
3. "Show suggestions" → list; "accept 1" → profile gains the fact (survives restart) / dismiss removes it from pending; a second scan does NOT re-propose an accepted/pending habit (no wasted Haiku call).
4. Below-threshold or single-day patterns produce nothing; a day with no qualifying habits spends $0.
5. `TTS_BACKEND=edge` → spoken replies use the neural voice; kill network → falls back to pyttsx3 (or text) without freezing; default (pyttsx3) unchanged.

## Self-review notes
- Spec coverage: derived-memory layer via nightly miner (§2.3/§3.3) → profile proposals with human accept/dismiss; free neural voice (§Phasing.5). Deliberately NOT built: event-correlation "rule" habits (schema/tooling reserved but miner emits profile-kind only — honest about the lack of a time-based trigger engine); phone/Telegram chat; Pi satellites.
- Cost: mining pure-Python; ≤1 Haiku call per NEW confirmed candidate, `source="habit"`, deduped by signature so re-scans of an open habit cost $0; edge-tts is $0 (network, not API).
- Concurrency: nightly tick + scan + surfacing all via `asyncio.to_thread`; never touch stores on the loop thread.
- Security: accepting a proposal writes profile/$0-direct-rule via write-tier tools through the guard+audit choke point; no new destructive surface; miner is read-only over the audit log.
- Cache discipline: proposal tools registered deterministically (whenever proposal_store present), appended last, stable order — no cache-prefix churn.
