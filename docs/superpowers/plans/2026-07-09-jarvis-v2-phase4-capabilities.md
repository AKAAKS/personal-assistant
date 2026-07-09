# JARVIS v2 Phase 4 — Capabilities Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give JARVIS web search + on-demand briefings, scoped file tools (`search_files`/`read_document`/`run_script`), meal tracking with CSV/JSON export, and the guard extensions that make those safe (path allowlist, secret-file hard block, first real destructive tool).

**Architecture:** All new tools follow the existing pattern — a module in `jarvisd/tools/` exporting `TOOLS` + `handlers(deps)`, registered in `build_registry`, permission-tiered in `security/policy.py`, executed only through `Assistant.execute_guarded`. Web search is the one exception: it is an Anthropic **server-side** tool that executes on Anthropic's infrastructure, so the guard cannot intercept it — cost control happens via `max_uses` per request plus a hard monthly cap enforced by dropping the tool from the request. Meals and the reminder-condition column ride a single schema v4 migration.

**Tech Stack:** Python 3.11, sqlite3 (existing conn), anthropic SDK (server web_search tool), pypdf + python-docx (new deps), pytest.

## Global Constraints

- Never commit `.env`, `logs/`, `data/`. Never print `.env` contents or API keys.
- Every locally-executed tool call goes through `execute_guarded` (guard + audit choke point). The `autonomous=True` semantics from Phase 3 are preserved untouched.
- Models: `claude-haiku-4-5` default, `claude-sonnet-5` complex (`jarvisd/core.py` HAIKU_MODEL/SONNET_MODEL).
- Cache discipline: breakpoints are (1) last **local** tool, (2) SYSTEM_PROMPT, (3) profile — max 4 allowed. The web_search server tool is appended AFTER the breakpoint tool so cap-tripping doesn't invalidate the cached prefix. Local tool list stays stable and deterministically ordered.
- Web search pricing pinned at implementation time: **$10 per 1,000 searches ($0.01/search)**, billed on top of tokens. Tool type is model-dependent: `web_search_20260209` on `claude-sonnet-5`; `claude-haiku-4-5` supports only the basic `web_search_20250305`.
- Tests: pytest, no network, in-memory SQLite via `jarvisd.db.connect(":memory:")`. Full suite green after every task. TDD: write the failing test first, see it fail, implement, see it pass.
- Windows paths throughout (`pathlib`, `os.path`); tests must not depend on a real `es.exe` or real user folders.

---

### Task 1: Cost tracker — web-search metering + hard monthly cap

**Files:**
- Modify: `router/cost_tracker.py`
- Test: `tests/test_cost_tracker.py` (append)

**Interfaces:**
- Consumes: existing `CostTracker.record(...)`, `logs/cost.jsonl` entry format.
- Produces: `record(..., web_search_requests: int = 0)` (new kwarg, added to cost at `WEB_SEARCH_PRICE_PER_CALL`, logged as `"search": N` in the jsonl entry); `search_requests_this_month() -> int`; `web_search_allowed() -> bool` (True while month-to-date searches < `self.search_monthly_cap`); constructor kwarg `search_monthly_cap: int = 100`.

- [ ] **Step 1: Failing tests** — append to `tests/test_cost_tracker.py` (match its existing tmp-path fixture style):

```python
def test_web_search_requests_priced_and_logged(tmp_path):
    t = CostTracker(log_path=str(tmp_path / "cost.jsonl"), monthly_budget=20.0)
    cost = t.record("claude-haiku-4-5", 1000, 100, web_search_requests=3)
    # 3 searches at $0.01 each on top of token cost
    token_only = t.record("claude-haiku-4-5", 1000, 100)
    assert cost == pytest.approx(token_only + 0.03)
    entry = json.loads((tmp_path / "cost.jsonl").read_text().splitlines()[0])
    assert entry["search"] == 3

def test_search_requests_this_month_counts_entries(tmp_path):
    t = CostTracker(log_path=str(tmp_path / "cost.jsonl"))
    t.record("claude-haiku-4-5", 10, 10, web_search_requests=2)
    t.record("claude-haiku-4-5", 10, 10)  # no searches
    t.record("claude-haiku-4-5", 10, 10, web_search_requests=1)
    assert t.search_requests_this_month() == 3

def test_web_search_allowed_respects_cap(tmp_path):
    t = CostTracker(log_path=str(tmp_path / "cost.jsonl"), search_monthly_cap=2)
    assert t.web_search_allowed() is True
    t.record("claude-haiku-4-5", 10, 10, web_search_requests=2)
    assert t.web_search_allowed() is False
```

- [ ] **Step 2:** Run `pytest tests/test_cost_tracker.py -q` — expect the 3 new tests FAIL (TypeError / AttributeError).
- [ ] **Step 3: Implement** in `router/cost_tracker.py`:

```python
# Pinned 2026-07: Anthropic server-side web_search bills $10 per 1,000 searches
# on top of tokens. Metered here; hard monthly cap enforced by core dropping
# the tool from requests (the guard cannot intercept server-side tools).
WEB_SEARCH_PRICE_PER_CALL = 10.00 / 1000
```

`__init__` gains `search_monthly_cap: int = 100`. `record()` gains `web_search_requests: int = 0`; add `web_search_requests * WEB_SEARCH_PRICE_PER_CALL` to `cost` and `"search": web_search_requests` to the entry (write the key always, 0 included, to keep the format uniform). `search_requests_this_month()` mirrors `get_monthly_spend()`'s scan (same month-key prefix match; treat missing `"search"` keys as 0; a separate `_search_month_cache` tuple invalidated in `record()` like `_month_cache`). `web_search_allowed()` returns `self.search_requests_this_month() < self.search_monthly_cap`.

- [ ] **Step 4:** `pytest tests/test_cost_tracker.py -q` — all pass. Full suite green.
- [ ] **Step 5:** Commit `feat: meter server-side web searches in cost tracker with hard monthly cap`

---

### Task 2: Schema v4 — meals table + reminder condition column; MealStore

**Files:**
- Modify: `jarvisd/db.py` (SCHEMA_VERSION 4, MIGRATIONS[4])
- Create: `jarvisd/meal_store.py`
- Test: `tests/test_meal_store.py` (new), `tests/test_db.py` (append migration test)

**Interfaces:**
- Consumes: `jarvisd.db.connect()` migration machinery.
- Produces: `MealStore(conn)` with:
  - `add(description: str, quantity: str, calories: int, protein_g: float, carbs_g: float, fat_g: float, confidence: str, assumptions: str) -> int` (stamps `ts` = now isoformat seconds)
  - `today_summary(now: datetime | None = None) -> dict` — `{"meals": [rows], "calories": int, "protein_g": float, "carbs_g": float, "fat_g": float}` for entries whose `ts` date == now's date
  - `logged_today(now: datetime | None = None) -> bool`
  - `export(fmt: str, out_dir: str = "data/exports") -> str` — writes `meals-YYYYMMDD-HHMMSS.csv` or `.json` (UTF-8, fmt in {"csv","json"} else ValueError), columns/keys: date, description, quantity, calories, protein_g, carbs_g, fat_g, confidence, assumptions; returns the path written.

- [ ] **Step 1:** Migration SQL (MIGRATIONS[4]):

```sql
CREATE TABLE meals (
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,
  description TEXT NOT NULL,
  quantity TEXT NOT NULL DEFAULT '',
  calories INTEGER NOT NULL,
  protein_g REAL NOT NULL,
  carbs_g REAL NOT NULL,
  fat_g REAL NOT NULL,
  confidence TEXT NOT NULL DEFAULT 'medium',
  assumptions TEXT NOT NULL DEFAULT ''
);
ALTER TABLE reminders ADD COLUMN condition TEXT;
```

- [ ] **Step 2: Failing tests.** In `tests/test_db.py`: fresh `connect(":memory:")` has `meals` table and `reminders.condition` column (PRAGMA table_info), and `schema_version` max == 4. In `tests/test_meal_store.py`: add→today_summary totals; meal added "yesterday" (insert row directly with a doctored ts) excluded from today; `logged_today` False on empty / True after add; export csv + json to `tmp_path` — file exists, csv has header + row, json parses, bad fmt raises ValueError. Run: expect FAIL.
- [ ] **Step 3:** Implement db.py bump + `jarvisd/meal_store.py` (mirror `reminder_store.py` style: conn in ctor, isoformat timestamps, `sqlite3.Row` results). `today_summary` filters with `WHERE ts LIKE date_prefix || '%'` where `date_prefix = (now or datetime.now()).date().isoformat()`. Export uses `csv` module / `json.dump` with `encoding="utf-8"` (cp1252 regression from Phase 2 — never open text files without an explicit encoding).
- [ ] **Step 4:** Tests pass; full suite green (existing reminder tests must be unaffected by the nullable column).
- [ ] **Step 5:** Commit `feat: schema v4 (meals + reminder condition) and MealStore`

---

### Task 3: Meal tools — log_meal / meal_summary / export_meals

**Files:**
- Create: `jarvisd/tools/meals.py`
- Modify: `jarvisd/tools/__init__.py` (registry), `security/policy.py` (permissions)
- Test: `tests/test_tools_meals.py` (new), `tests/test_tools_registry.py` (append)

**Interfaces:**
- Consumes: `MealStore` from Task 2.
- Produces: tool defs + `handlers(meal_store)`. **Design point: the MODEL supplies the macro estimates as tool input** — no extra API call, honest about being an LLM guess. `log_meal` input_schema requires `description`, `calories` (integer), `protein_g`, `carbs_g`, `fat_g` (numbers), `confidence` (enum low/medium/high); optional `quantity`, `assumptions`. Description text must say: "Estimate the macros yourself from the description; state portion assumptions in `assumptions` and your confidence honestly. These are estimates for trend tracking, not a food database." `meal_summary` (no args) returns "Today: N meals, X kcal, P g protein, C g carbs, F g fat" plus one line per meal (`{id}. {description} — {calories} kcal`); "No meals logged today." when empty. `export_meals` takes `format` (enum csv/json, default csv), returns "Exported meal log to {path}".
- Policy: `log_meal: "write"`, `meal_summary: "read_only"`, `export_meals: "write"`.
- Registry: `if meal_store is not None:` block appended after rules (order: ha, system, spotify?, tv?, memory?, reminders?, rules?, meals?) — new params always append so existing order is stable.

- [ ] **Step 1: Failing tests** — mirror `tests/test_tools_proactive.py` structure: schema well-formedness; log→summary round trip shows totals and real DB ids; missing required field returns "Error: ..." string; export writes file under `tmp_path` (pass `out_dir` through handler? No — handler calls `meal_store.export(fmt)`; in the test monkeypatch `meal_store.export` or construct store and point export at tmp via a store-level default-arg call `export(fmt, out_dir=...)` — handlers use the store default; test the handler with a store whose export is spied). Registry test: `build_registry(..., meal_store=store)` includes the 3 tools and handlers; `meal_store=None` excludes them.
- [ ] **Step 2:** RED. **Step 3:** Implement (handlers validate presence/types minimally, return error strings — never raise; follow `reminders.py` idiom).
- [ ] **Step 4:** GREEN, full suite. **Step 5:** Commit `feat: meal tracking tools (log/summary/export)`

---

### Task 4: Standing-nudge condition on reminders

**Files:**
- Modify: `jarvisd/reminder_store.py` (`add(..., condition: str | None = None)`, row exposes it), `jarvisd/server.py` (`_fire_due_reminders` condition check), `jarvisd/tools/reminders.py` (`set_reminder` optional `condition` input, enum `["meal_not_logged_today"]`), `jarvisd/__main__.py` + `jarvisd/server.py` ctor (server needs `meal_store`)
- Test: `tests/test_reminder_store.py`, `tests/test_server_proactive.py` (append)

**Interfaces:**
- Consumes: `MealStore.logged_today()`, schema v4 `condition` column.
- Produces: `JarvisServer(..., meal_store=None)`. In `_fire_due_reminders`, before notifying: if `r["condition"] == "meal_not_logged_today"` and `self.meal_store is not None` and `await asyncio.to_thread(self.meal_store.logged_today)` → still `mark_fired` (advance recurrence) but skip the notify. Unknown condition values: log a warning, fire anyway (fail-open — a typo must not silently kill a reminder). Spec example: "8:30pm nudge to log dinner *unless* a meal was logged."

- [ ] **Step 1: Failing tests.** Store: add with condition round-trips; default None. Server (async tests in `test_server_proactive.py` style, spy notifier): due reminder with condition + meal logged today → notify NOT called, mark_fired called; condition + no meal → notify called; condition set but `meal_store=None` → notify called (fail-open).
- [ ] **Step 2:** RED. **Step 3:** Implement. `set_reminder` tool description gains: "Optional condition: 'meal_not_logged_today' makes the reminder fire only if no meal has been logged that day (a standing nudge)."
- [ ] **Step 4:** GREEN, full suite. **Step 5:** Commit `feat: conditional reminders (meal-not-logged nudge)`

---

### Task 5: Web search + briefing in core

**Files:**
- Modify: `jarvisd/core.py`
- Test: `tests/test_core_memory.py` (append — it already has `FakeAnthropicClient`/`fake_response` fakes)

**Interfaces:**
- Consumes: `CostTracker.web_search_allowed()` + `record(web_search_requests=...)` from Task 1.
- Produces in `jarvisd/core.py`:

```python
WEB_SEARCH_MAX_USES_PER_REQUEST = 3

def web_search_tool(model: str) -> dict:
    """Newest server-side web_search variant the model supports (spec §4).

    claude-sonnet-5 supports web_search_20260209 (dynamic filtering);
    claude-haiku-4-5 only supports the basic web_search_20250305.
    """
    variant = "web_search_20260209" if model == SONNET_MODEL else "web_search_20250305"
    return {"type": variant, "name": "web_search", "max_uses": WEB_SEARCH_MAX_USES_PER_REQUEST}
```

- `Assistant.__init__` gains `enable_web_search: bool = False` (bootstrap turns it on; tests default off so every existing test is unaffected).
- In `_claude_loop`, build the per-request tool list: `tools = self.tools` and, if `self.enable_web_search and self.cost_tracker.web_search_allowed()`, `tools = self.tools + [web_search_tool(model)]`. The server tool is appended AFTER `self.tools[-1]` (which carries the cache breakpoint), so cap-tripping mid-month does not invalidate the cached tools prefix.
- Usage recording: `srv = getattr(u, "server_tool_use", None)`; `searches = getattr(srv, "web_search_requests", 0) or 0 if srv else 0`; pass `web_search_requests=searches` to `cost_tracker.record`.
- **`pause_turn` handling** (required for server-side tools): after appending `assistant_content` to the conversation, `if response.stop_reason == "pause_turn": continue` — re-request; the API resumes the server-side loop. Guard against infinite pauses with a counter (max 5 continuations → return the text parts joined, or "(web search did not complete)" if empty).
- SYSTEM_PROMPT additions: under Capabilities, `- **Web search**: current information from the web, with citations. Each search costs money — search only when the answer needs current information.` and a Guidelines line: `- "Give me my briefing" = weather + top headlines (web search) + today's reminders (list_reminders) + today's meals (meal_summary).`

- [ ] **Step 1: Failing tests** (extend the fakes in `test_core_memory.py`; fakes' `messages.create` must capture `tools` and accept/expose `stop_reason` — extend `fake_response(...)` with `stop_reason="end_turn"` default):
  - `test_web_search_tool_variant_per_model` — 20260209 for SONNET_MODEL, 20250305 for HAIKU_MODEL, `max_uses` present.
  - `test_web_search_appended_after_cached_local_tools` — with `enable_web_search=True` and a permissive fake tracker, captured `tools[-1]["name"] == "web_search"` and `tools[-2]` carries `cache_control` (the local breakpoint is untouched).
  - `test_web_search_dropped_when_cap_hit` — fake tracker `web_search_allowed() -> False`: no web_search in captured tools.
  - `test_server_tool_usage_recorded` — fake response usage carries `server_tool_use=SimpleNamespace(web_search_requests=2)`; spy tracker receives `web_search_requests=2`.
  - `test_pause_turn_continues_loop` — first fake response `stop_reason="pause_turn"` (content: one text block), second `end_turn` with final text; loop returns final text and made 2 create calls.
- [ ] **Step 2:** RED. **Step 3:** Implement. **Step 4:** GREEN + full suite (existing `test_core*`/`test_server*` must stay green — `enable_web_search` defaults False).
- [ ] **Step 5:** Commit `feat: server-side web search with monthly cap, pause_turn handling, briefing prompt`

---

### Task 6: Path security — allowlist + secret-file block in the guard

**Files:**
- Create: `security/paths.py`
- Modify: `security/policy.py`, `security/guard.py`
- Test: `tests/test_paths.py` (new), `tests/test_guard.py` (append)

**Interfaces:**
- Produces `security/paths.py`:

```python
"""Filesystem allowlist for file tools. Roots come from FILE_ROOTS env
(semicolon-separated) or default to ~/Documents;~/Projects;~/Downloads."""
DEFAULT_ROOTS = ["~/Documents", "~/Projects", "~/Downloads"]

def allowed_roots() -> list[Path]      # expanduser().resolve() each; env override via FILE_ROOTS
def is_allowed_path(path: str) -> bool # Path(path).expanduser().resolve() is-relative-to any root
def is_secret_path(path: str) -> bool  # case-insensitive name match against SECRET_FILE_PATTERNS

SECRET_FILE_PATTERNS = [  # matched against the basename, lowercased
    ".env", "*.pem", "*.key", "*.pfx", "*.p12", "id_rsa*", "id_ed25519*",
    "*credential*", "*secret*", "*token*.json", ".netrc", ".npmrc", ".pypirc",
]
```

`is_secret_path` uses `fnmatch.fnmatch(name.lower(), pat)` — and also returns True if ANY path component matches `.env` exactly (blocks `proj/.env` read via directory tricks). `resolve()` handles `..` and symlinks (canonicalize before checking — path traversal like `C:/Users/x/Documents/../../Windows` must fail).
- Policy additions: `PATH_CHECKED_TOOLS = {"search_files": ["directory"], "read_document": ["path"], "run_script": []}` (tool → input keys holding user paths; run_script's path is whitelist-constrained by the tool itself, not user-supplied) and `TOOL_PERMISSIONS` entries: `search_files: "read_only"`, `read_document: "read_only"`, `run_script: "destructive"`, `log_meal/meal_summary/export_meals` (if not already from Task 3).
- Guard: in `check()`, after the URL check and before permission lookup, if `tool_name in PATH_CHECKED_TOOLS`: for each configured key present and non-empty in `tool_input`, BLOCK if `is_secret_path(value)` ("Blocked: access to secret-shaped files is not permitted.") or if not `is_allowed_path(value)` ("Blocked: path is outside the allowed folders (Documents, Projects, Downloads)."). Additionally — belt and braces, ALL tools: if any string anywhere in `tool_input` (reuse `_extract_strings`) looks like a path (`re.search(r"[\\/]", s)`) AND `is_secret_path(s)` → BLOCK. This is the spec's "hard block on secret-shaped files everywhere".

- [ ] **Step 1: Failing tests.** `test_paths.py` (monkeypatch `FILE_ROOTS` to two `tmp_path` roots): inside-root allowed; outside blocked; `..` traversal out of a root blocked; `.env`/`id_rsa`/`server.pem`/`spotify_tokens.json`/`my-credentials.txt` secret-flagged; `notes.md` not. `test_guard.py`: `read_document` outside roots → BLOCK; inside → ALLOW (read_only); `read_document` of `<root>/proj/.env` → BLOCK; **any** tool with a `.env`-ish path string in input → BLOCK (e.g. `send_notification {"message": "C:/proj/.env"}`); `run_script` is destructive → CONFIRM_REQUIRED then ALLOW on retry, and autonomous stays terminal CONFIRM_REQUIRED (extends the Phase 3 fix's coverage to the first real destructive tool).
- [ ] **Step 2:** RED. **Step 3:** Implement. **Step 4:** GREEN + full suite (watch: the everywhere-block must not trip on URLs — `https://...` contains `/` but won't fnmatch secret patterns; keep patterns basename-scoped).
- [ ] **Step 5:** Commit `feat: guard path allowlist + secret-file hard block`

---

### Task 7: File tools — search_files + read_document

**Files:**
- Create: `jarvisd/tools/files.py`
- Modify: `jarvisd/tools/__init__.py`, `jarvisd/bootstrap.py` (summarizer callable), `requirements.txt` (`pypdf`, `python-docx`)
- Test: `tests/test_tools_files.py` (new)

**Interfaces:**
- Consumes: `security/paths.py` helpers (tools ALSO check paths for good UX errors; the guard remains the enforcement backstop), summarizer callable injected by bootstrap.
- Produces: `files.TOOLS` + `files.handlers(summarizer=None)`:
  - `search_files(query, directory=None)` — if `shutil.which("es")`: run `["es", "-n", "30", *(["-path", directory] if directory else []), query]` (`subprocess.run`, `capture_output=True`, `text=True, encoding="utf-8", errors="replace"`, `timeout=10`); else fallback `os.walk` over `allowed_roots()` (or the one directory), case-insensitive substring match on filename, cap 30 hits, skip dirs named `.git`, `node_modules`, `__pycache__`, `.venv`. Filter every result line/path through `is_allowed_path` and drop `is_secret_path` hits (es.exe searches the whole disk — results MUST be post-filtered). Returns newline list or "No files found matching '...'".
  - `read_document(path)` — ext-dispatch: `.txt/.md/.py/.log/...` (any non-binary default) → `Path.read_text(encoding="utf-8", errors="replace")`; `.pdf` → pypdf `PdfReader`, join page `.extract_text()`; `.docx` → python-docx `Document`, join paragraph texts. Size cap: refuse files > 5 MB ("Error: file too large"). If extracted text ≤ 6000 chars → return raw. Else: if summarizer is not None → `summarizer(text[:30000])` returned as "Summary of {name}:\n{summary}"; else return first 6000 chars + "\n...[truncated]".  Import pypdf/docx lazily inside the handler; missing dep → "Error: pypdf not installed" (string, never raise).
- Bootstrap summarizer (in `create_assistant`, after `ai` exists):

```python
def summarizer(text: str) -> str:
    """Condense long documents: free local Ollama when up, else one Haiku call."""
    prompt = "Summarize the key points of this document in <=300 words:\n\n" + text
    if ai.ollama.is_available():
        try:
            return ai.ollama.generate(prompt)
        except Exception:
            pass
    resp = ai.client.messages.create(
        model=HAIKU_MODEL, max_tokens=600,
        messages=[{"role": "user", "content": prompt}],
    )
    u = resp.usage
    ai.cost_tracker.record(HAIKU_MODEL, u.input_tokens, u.output_tokens, source="summarize")
    return "".join(b.text for b in resp.content if b.type == "text")
```

  (import HAIKU_MODEL from jarvisd.core; wire `files.handlers(summarizer=summarizer)` — registry gains a `file_summarizer=None` param; file tools registered whenever `file_summarizer` is passed OR unconditionally with `summarizer=None`? **Decision: registry takes `file_tools: bool = False` + `file_summarizer=None`; bootstrap passes both.** Keeps tests explicit.)
- Policy entries came in Task 6.

- [ ] **Step 1: Failing tests** (all under `tmp_path` roots via `FILE_ROOTS` monkeypatch; force fallback path by monkeypatching `shutil.which` → None): search finds a created file by substring, case-insensitive; respects 30-cap; skips `node_modules`; drops a `.env` planted in the root; returns friendly no-match string. read_document: reads a UTF-8 .md with emoji; refuses > 5 MB (write a sparse big file or monkeypatch `Path.stat`); long text with summarizer spy → spy called, "Summary of" in result; long text without summarizer → truncated marker; missing file → error string; `.docx`/`.pdf` handlers exercised only if libs importable (`pytest.importorskip`), else the missing-dep error-string branch is asserted by monkeypatching the import.
- [ ] **Step 2:** RED. **Step 3:** Implement + add `pypdf` and `python-docx` to `requirements.txt` + `pip install pypdf python-docx`.
- [ ] **Step 4:** GREEN + full suite. **Step 5:** Commit `feat: search_files + read_document with allowlist enforcement and summarizer`

---

### Task 8: run_script — first real destructive tool

**Files:**
- Modify: `jarvisd/tools/files.py` (add tool + handler), `tests/test_tools_files.py`, `tests/test_core.py` (e2e confirm flow)
- Create: `scripts/jarvis_approved/README.txt` (one line: "Scripts in this folder can be run by JARVIS after voice confirmation. Review anything you drop here.")

**Interfaces:**
- Produces: `run_script(name, args=None)` tool. Whitelist dir: `scripts/jarvis_approved/` (env override `JARVIS_SCRIPTS_DIR`). Handler: `list` available scripts when `name` missing/not found ("Available scripts: ..."). Resolve `name` against the dir; reject any `name` containing path separators or `..` (defense in depth — resolved path must be inside the dir). Dispatch by suffix: `.py` → `[sys.executable, path, *args]`, `.bat`/`.cmd` → `["cmd", "/c", path, *args]`, `.ps1` → `["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", path, *args]`; else "Error: unsupported script type". `subprocess.run(..., capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)`; return exit code + stdout tail (last 2000 chars) + stderr tail; `TimeoutExpired` → error string. `args` schema: array of strings, optional.
- Policy: `run_script: "destructive"` (Task 6 already added it — verify).

- [ ] **Step 1: Failing tests.** Tools tests (monkeypatch `JARVIS_SCRIPTS_DIR` to `tmp_path`): runs a trivial `.py` that prints marker → output contains marker + "exit 0"; unknown name lists available; `name="../evil.py"` rejected; unsupported ext rejected; timeout path (script sleeping > patched timeout — monkeypatch the module's `SCRIPT_TIMEOUT_SEC` to 1). Core e2e (extends `test_core.py`, SpyAudit-style, no logs writes): `execute_guarded("run_script", {"name": "x.py"})` → first call CONFIRMATION REQUIRED + handler NOT executed; identical second call executes; `autonomous=True` never executes (this is the full spec'd confirm-before-execute path exercised end-to-end with a real policy entry, not a monkeypatched fake).
- [ ] **Step 2:** RED. **Step 3:** Implement. **Step 4:** GREEN + full suite.
- [ ] **Step 5:** Commit `feat: run_script whitelist-folder tool behind guard confirm flow`

---

### Task 9: Wiring + README + ledger

**Files:**
- Modify: `jarvisd/bootstrap.py`, `jarvisd/__main__.py`, `jarvisd/core.py` (Assistant accepts `meal_store`, `enable_web_search` — verify Task 5 landed the latter), `jarvisd/tools/__init__.py` (verify all three new modules registered), `README.md`, `.env.example` (SEARCH_MONTHLY_CAP, FILE_ROOTS, JARVIS_SCRIPTS_DIR — commented defaults)
- Test: import check + full suite (wiring is covered by the per-module tests; add one bootstrap test only if `create_assistant` signature changed again)

**Interfaces:**
- `create_assistant()`: build `MealStore(conn)` in the same all-or-nothing memory try/except; pass `meal_store` + `enable_web_search=True` + summarizer into Assistant/registry; return tuple extended with `meal_store` (callers: `__main__.run` — pass `meal_store` into `JarvisServer`). `CostTracker` constructed with `search_monthly_cap=int(os.getenv("SEARCH_MONTHLY_CAP", "100"))` — check where CostTracker is built (inside Assistant.__init__) → Assistant gains `search_monthly_cap` param or reads env directly in core; **pick: read env in Assistant.__init__ when constructing CostTracker** (matches existing monthly_budget pattern? monthly_budget is a param — mirror it: `search_monthly_cap: int = 100` param, bootstrap passes the env value).
- Features line: append `"web search"`, `"file tools"`, `"meals"` when wired.
- README sections: **Web search & briefing** ("give me my briefing"; SEARCH_MONTHLY_CAP env, $0.01/search note), **Files** (three tools, allowed roots + FILE_ROOTS, secret-file block, run_script whitelist folder + confirm flow: say "yes, run it" and the assistant retries), **Meal tracking** (log by voice, "how am I doing today", export → data/exports, honest-LLM-estimate caveat).
- Ledger: append Phase 4 section to `.superpowers/sdd/progress.md`.

- [ ] **Step 1:** Wire bootstrap/__main__/core params. **Step 2:** `python -c "from jarvisd.bootstrap import create_assistant; import jarvisd.__main__; print('OK')"`. **Step 3:** README + .env.example. **Step 4:** Full suite incl. slow green.
- [ ] **Step 5:** Commit `feat: wire Phase 4 capabilities into daemon + README`

---

## Final acceptance checklist (manual, after merge)

1. `pytest` all green; daemon restart clean.
2. "What's the weather in <city> right now?" → web search fires, citations in reply, `cost.jsonl` entry has `"search" >= 1`.
3. "Give me my briefing" → weather + headlines + reminders + meals in one reply.
4. "Log a meal: two eggs and toast with butter" → macros with confidence + assumptions; "how am I doing today" → totals; "export my meal log" → file in `data/exports/`.
5. "Find my file about <keyword>" → hits from Documents/Projects/Downloads only; "read <doc>" summarizes a long PDF.
6. Ask to read `.env` → security block, audited.
7. Drop `hello.py` in `scripts/jarvis_approved/`, ask JARVIS to run it → asks for confirmation → "yes, run it" → executes; a trigger rule attempting run_script stays CONFIRMATION REQUIRED (autonomous path).
8. Reminder "remind me at 8:30pm to log dinner unless I already did" → skipped on a day with a logged meal.

## Self-review notes

- Spec coverage: web search + briefing (T1/T5), search cap metering (T1), file tools ×3 (T7/T8), guard path-allowlist + secret block (T6), meal log/summary/export + evening-nudge condition (T2/T3/T4), `run_script` destructive/confirm (T8), wiring+docs (T9). Habit miner + edge-tts are Phase 5, deliberately absent.
- Type consistency: `web_search_allowed()`/`search_requests_this_month()`/`record(web_search_requests=)` names used identically in T1 and T5; `logged_today()` in T2 and T4; `is_allowed_path`/`is_secret_path` in T6/T7.
- Cache discipline: web_search appended after the breakpoint; local tool list unchanged per model; cap-flip affects only the un-cached suffix.
- Guard cannot see server-side tools — cap enforcement is pre-request by construction (T5), documented in code comment (T1).
