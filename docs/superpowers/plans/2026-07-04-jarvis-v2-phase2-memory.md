# JARVIS v2 Phase 2 — Memory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give JARVIS persistent memory: a curated profile injected (and prompt-cached) into every call, `remember`/`recall` tools, episodic storage of conversations in SQLite FTS5, and conversation history that survives daemon restarts. Spec: `docs/superpowers/specs/2026-07-02-jarvis-v2-design.md` §2, §5.

**Architecture:** Two new small modules in `jarvisd/` (`memory_store.py` for SQLite episodic memory, `profile_store.py` for the profile file), one new tools module (`jarvisd/tools/memory.py`), a v2 DB migration in `jarvisd/db.py`, and integration into `Assistant` (system-prompt assembly + turn persistence + history reload) and `bootstrap`.

**Tech stack:** Python 3.11, stdlib sqlite3 with FTS5 (bundled in CPython Windows builds), pytest. No new dependencies.

## Global Constraints

- Windows 11; commands below are Git Bash. Working dir: `C:/Users/akshi/Projects/personal-assistant`.
- All work on branch `phase2-memory` off `master`. TDD: each task writes failing tests first (RED), then implements (GREEN); implementer reports both.
- Never commit `.env`, `logs/`, `data/` (profile + DB are personal data and live in `data/`).
- Tests: plain `pytest` from repo root; no network, no ML models; SQLite tests use `tmp_path`.
- Models/pricing/budget machinery untouched.
- **Profile location:** `data/profile.md` (spec says `memory/profile.md`; we use `data/` because it is already gitignored and holds all personal state — document this deviation in README).
- **Cache discipline (spec §5):** prompt prefix order is tools → SYSTEM_PROMPT → profile, with cache breakpoints on the last tool (exists), SYSTEM_PROMPT (exists), and the profile block (new, LAST) so profile edits invalidate only the profile segment. Never put the profile before the system prompt.
- **Threading:** `db.connect()` must pass `check_same_thread=False`. The connection is created on the main thread but used from `asyncio.to_thread` worker threads; the server's `asyncio.Lock` already serializes all assistant access, so this is safe.

---

### Task 0: Branch

**Files:** none

- [ ] **Step 1:** `git checkout -b phase2-memory`

---

### Task 1: DB migration v2 + MemoryStore (episodic memory)

**Files:**
- Modify: `jarvisd/db.py` (migration 2, `check_same_thread=False`)
- Create: `jarvisd/memory_store.py`
- Create: `tests/test_memory_store.py`

**Interfaces:**
- Produces: `MemoryStore(conn)` with:
  - `record(role: str, text: str) -> None` — store one conversational turn (role `"user"`/`"assistant"`, plain text only). Empty/whitespace text is not stored.
  - `search(query: str, limit: int = 5) -> list[sqlite3.Row]` — FTS5 match over content, newest first; rows have `ts`, `role`, `content`. Must not raise on FTS5 query syntax errors (quote the user query — wrap in `'"' + query.replace('"', '""') + '"'` for a literal phrase-ish match, or catch `sqlite3.OperationalError` and fall back to a LIKE query).
  - `recent(n: int = 20) -> list[sqlite3.Row]` — last n rows in chronological order.
  - `load_history(max_turns: int = 10) -> list[dict]` — recent rows converted to Anthropic messages `[{"role": ..., "content": ...}]`, guaranteed valid: starts with `user`, strictly alternates (merge consecutive same-role rows by joining with `\n`), ends with `assistant` (drop a trailing unanswered user turn).

- [ ] **Step 1 (RED):** Write `tests/test_memory_store.py` against a `tmp_path` DB: record/search round-trip; search is newest-first and respects limit; hostile query strings (`'weird "quotes" AND (', 'foo*'`) don't raise; `recent` ordering; `load_history` alternation rules (consecutive user rows merge; trailing user row dropped; empty DB → `[]`); timestamps are ISO local time seconds. Also: `db.connect(tmp)` twice is idempotent and `schema_version` reaches 2.
- [ ] **Step 2 (GREEN):** Migration 2 in `db.py`:

```sql
CREATE TABLE messages (
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,
  role TEXT NOT NULL,
  content TEXT NOT NULL
);
CREATE VIRTUAL TABLE messages_fts USING fts5(content, content=messages, content_rowid=id);
CREATE TRIGGER messages_ai AFTER INSERT ON messages BEGIN
  INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
END;
CREATE TRIGGER messages_ad AFTER DELETE ON messages BEGIN
  INSERT INTO messages_fts(messages_fts, rowid, content) VALUES ('delete', old.id, old.content);
END;
```

Bump `SCHEMA_VERSION` to 2. Add `check_same_thread=False` to `sqlite3.connect`. Implement `MemoryStore` per the interface; `ts` = `datetime.now().isoformat(timespec="seconds")`.

- [ ] **Step 3:** Commit `feat: episodic memory store (SQLite FTS5)`.

---

### Task 2: ProfileStore

**Files:**
- Create: `jarvisd/profile_store.py`
- Create: `tests/test_profile_store.py`

**Interfaces:**
- Produces: `ProfileStore(path: str = "data/profile.md")` with:
  - `load() -> str` — file contents; `""` if missing. Caches by mtime: re-reads only when the file changed (user edits take effect without restart).
  - `append(fact: str) -> str` — appends `- {fact}` (single line, newlines in fact collapsed to spaces) under the file, creating it with a header (`# JARVIS profile — facts the assistant always knows\n\n`) and parent dir if missing. Returns a status string: `"Remembered: {fact}"`, plus the warning `" (profile is over ~1500 tokens — consider pruning data/profile.md)"` when `estimated_tokens() > 1500`.
  - `estimated_tokens() -> int` — `len(load()) // 4`.

- [ ] **Step 1 (RED):** Tests with `tmp_path`: load missing → `""`; append creates file with header; append returns "Remembered:"; mtime caching (write via another handle → load() picks it up; monkeypatch-free: just check content freshness); over-cap warning triggers (append a long fact to a pre-filled file); newlines collapsed.
- [ ] **Step 2 (GREEN):** Implement. Keep it dumb — no locking (single daemon process owns writes).
- [ ] **Step 3:** Commit `feat: profile store (curated always-known facts)`.

---

### Task 3: remember/recall tools + registry + guard policy

**Files:**
- Create: `jarvisd/tools/memory.py`
- Modify: `jarvisd/tools/__init__.py` (registry accepts the stores)
- Modify: `security/policy.py` (permissions for the two tools)
- Create: `tests/test_tools_memory.py`
- Modify: `tests/test_tools_registry.py` (registry with/without stores)

**Interfaces:**
- `jarvisd/tools/memory.py` exports `TOOLS` (Anthropic definitions) and `handlers(profile_store, memory_store) -> dict`, matching the existing per-domain module pattern (read `jarvisd/tools/system.py` for the shape).
  - `remember` — input `{"fact": str}` (required). Description: store a lasting fact about the user/home (preferences, nicknames, standing instructions) — NOT transient events. Handler: `profile_store.append(fact)`, return its status string.
  - `recall` — input `{"query": str}` (required), `{"limit": int}` (optional, default 5). Description: search past conversations. Handler formats matches as `[{ts}] {role}: {content}` lines (content truncated to 200 chars), or `"No memories matching '{query}'."`.
- `build_registry(ha_client, system_client, spotify_client=None, tv_client=None, profile_store=None, memory_store=None)` — memory tools included only when BOTH stores are provided. **Keep the memory tools' position deterministic: append them last, after tv.** (The last-tool cache breakpoint in `core.py` then covers them automatically.)
- `security/policy.py`: `"remember": "write"`, `"recall": "read_only"`.

- [ ] **Step 1 (RED):** Tests: handlers round-trip against real stores on `tmp_path`; recall no-match message; registry excludes memory tools when stores are None (existing calls unchanged — backward compat) and includes them last when provided; policy has the two entries with the right tiers.
- [ ] **Step 2 (GREEN):** Implement.
- [ ] **Step 3:** Commit `feat: remember/recall tools wired into registry + guard policy`.

---

### Task 4: Core integration — profile in prompt, turn persistence, history reload

The judgment task: touches the money path. Modify `jarvisd/core.py` surgically; do not restructure anything else.

**Files:**
- Modify: `jarvisd/core.py`
- Create: `tests/test_core_memory.py` (mock the Anthropic client — copy the fake pattern from `tests/test_core.py`)

**Changes to `Assistant`:**

1. `__init__` gains `profile_store=None, memory_store=None`; pass both to `build_registry`. Replace the static `self._system_prompt` with a `_build_system()` method:
   - Block 1: `SYSTEM_PROMPT` with `cache_control` (unchanged).
   - Block 2 (only if profile_store and profile text non-empty): `f"# What you know about the user\n\n{profile}"` with `cache_control: ephemeral`.
   - Called every `_claude_loop` iteration is wasteful only when the profile changed — `ProfileStore.load()` is mtime-cached, so calling `_build_system()` per API request is cheap. Do that; no extra caching layer in core.
2. Turn persistence (only when `memory_store` is set): in `process()`, record the user message once per call (`memory_store.record("user", user_message)`) — including the local-tier path — and record the final assistant text (`record("assistant", reply)`) on every return path (local + `_claude_loop` final text). Do NOT record tool_use/tool_result blocks. Persistence failures must never break a reply: wrap records in try/except logging a warning.
3. History reload: `__init__` ends with: if `memory_store`, `self.conversation = memory_store.load_history(max_turns=10)`, and log `"Restored %d turns of conversation history"` when non-empty.
4. `reset_conversation()` unchanged (RAM only — episodic memory is the point of recall).

- [ ] **Step 1 (RED):** Tests with fake Anthropic client + tmp stores: system param passed to `messages.create` contains the profile block last with `cache_control` when profile exists, and only one block when no profile; user+assistant turns persisted for both local-tier and Claude-tier replies; a `record` that raises doesn't break the reply; history restored into `self.conversation` on construction; restored history is valid alternation.
- [ ] **Step 2 (GREEN):** Implement. Existing `tests/test_core.py` must stay green untouched.
- [ ] **Step 3:** Commit `feat: profile-aware prompt, persistent turns, history reload`.

---

### Task 5: Bootstrap wiring + README

**Files:**
- Modify: `jarvisd/bootstrap.py`
- Modify: `README.md`

- [ ] **Step 1:** In `create_assistant()`: `conn = db.connect(os.getenv("JARVIS_DB", "data/jarvis.db"))`; build `MemoryStore(conn)` and `ProfileStore()`; pass both to `Assistant`. Add `"Memory"` to `features`. DB/bootstrap failure must not kill the daemon: wrap in try/except → log warning, continue with both stores None.
- [ ] **Step 2:** README: add a Memory section — profile lives at `data/profile.md` (user-editable, takes effect immediately), "remember that ..." stores facts, "what did we talk about ..." recalls, history survives restarts; note the spec-path deviation.
- [ ] **Step 3:** Run full suite (`pytest`), commit `feat: wire memory into bootstrap + README`.

---

## Final acceptance checklist (manual, ~5 min)

1. `pytest` all green.
2. Restart daemon → log shows "Restored N turns"; ask "what was the last thing I asked you?" → answers from restored history.
3. "Remember that I like my lights at 40% in the evening" → `data/profile.md` gains the bullet; restart daemon; "what brightness do I like?" → answered from profile without recall.
4. "What did we talk about regarding volume?" → recall tool surfaces the speaker conversation.
5. `logs/cost.jsonl`: interactive calls show `cache_read > 0` from the second call on (tools+system+profile now clear Haiku's cacheable minimum).
