# Macro Chef — Logging + Daily Status Implementation Plan (Plan 3 of 5)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace JARVIS's LLM-estimated meal log with a DB-sourced one: log a meal by naming its ingredients (each a DB/pantry food + grams) and let the deterministic engine compute every macro from per-100g snapshots; auto-deplete the pantry when a meal is cooked; log eating-out meals honestly as *estimated* (DB per-100g × eyeballed grams, flagged, never depleting); and show a MacroFactor-grade daily status that splits verified vs estimated against the day's target.

**Architecture:** All macro arithmetic stays in `nutrition_core` (engine + repo), never in the LLM or the tool layer — the tool passes ingredient names + grams, the engine returns the numbers (zero-error). A shared *ingredient resolver* turns an LLM-supplied ingredient spec (barcode / name / pantry-id + grams) into a `MealIngredient` carrying a real per-100g snapshot, or fails the whole meal with a clear message (never logs a guessed number). Verified logs deplete the pantry via Plan 2's `adjust_quantity`; estimated logs don't. A new repo `day_status(conn, date)` does the split + budget math; the `day_status` tool only formats it.

**Tech Stack:** Python 3.11, existing `nutrition_core` (engine/budget/models/store), `FoodService` (Plan 1), `PantryStore`/pantry repo (Plan 2), JARVIS tool-registry pattern, SQLite (`data/jarvis.db`). No new dependency.

**Spec:** `docs/superpowers/specs/2026-10-31-macro-chef-tool-pack-design.md` (meal model, three log paths, verified-vs-estimated split, status). Builds on Plan 1 (food lookup) + Plan 2 (pantry), both merged.

## Global Constraints

- **Python 3.11.** No new dependency.
- **Zero-error (binding):** the LLM never supplies a final macro total. Every logged number is `grams × per-100g` computed by `nutrition_core.engine` from a DB/pantry/label snapshot. Estimation is confined to the *grams* of an estimated meal and is always flagged (`status='estimated'`, honest confidence). If an ingredient cannot be resolved to a real per-100g snapshot, the **whole meal is rejected and nothing is logged** — never a partial or guessed log.
- **Status vocabulary:** verified/cooked meals → `status='verified'`; eating-out estimates → `status='estimated'`. The verified-vs-estimated split keys on `status == 'estimated'` (so legacy LLM rows, which carry the table default `'estimated'`, correctly fall in the estimated bucket).
- **Pantry depletion:** only verified logs deplete, and depletion is **best-effort** — a depletion failure never un-logs a successfully logged meal (log first, then deplete, warn on failure). Estimated logs never touch the pantry. Depleting more than is tracked floors at 0 (never negative).
- **Tools never raise** — each returns a human-readable string, including on every bad input (follow `jarvisd/tools/pantry.py` / `food.py`: validate, friendly strings, outer catch-all). New `build_registry`/`Assistant` params default `None` (non-regression).
- **Dates** are the local date (`datetime.now().date().isoformat()`); day filters use `ts LIKE '<date>%'` (works for both date-only verified rows and ISO-8601 legacy rows).
- **No live network in tests** — fake `FoodService`/adapters; real in-memory DB via `from jarvisd.db import connect; connect(":memory:")`.
- Commits end: `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.

## Review Focus

1. **One bad ingredient rejects the whole meal** — if any ingredient fails to resolve (barcode miss, no name match, unknown pantry id, bad grams), the log tool writes NO meal row, NO ingredient rows, and depletes NOTHING, and says which ingredient failed. *(Task 3.)*
2. **Depletion failure must not un-log a good meal** — the meal is logged first; a later `adjust_quantity` raising (locked row) leaves the meal logged and returns success with a warning, not an error. *(Task 3.)*
3. **Over-depletion floors, never negative** — cooking 200 g of an item with only 150 g tracked removes the row (0 g), never stores a negative, and the log still succeeds. *(Task 2 for the primitive; Task 3 for the end-to-end.)*
4. **`day_status` with no target set** — returns consumed totals + the verified/estimated split with `remaining`/`assessment` as None, and the tool says "no daily target set" gracefully rather than dividing by zero or crashing. *(Tasks 1 & 4.)*
5. **Estimated contribution is never hidden** — a day whose total is partly estimated shows the estimated share explicitly in `day_status`; an over-budget day surfaces the over/cornered assessment. *(Tasks 1 & 4.)*

---

### Task 1: Meals repo — flaggable logging + `day_status`

**Files:**
- Modify: `nutrition_core/store/meals.py`
- Test: `tests/test_nutrition_store_day_status.py`

**Interfaces:**
- Consumes: `compute_ingredient`/`compute_meal` (engine), `Macros`/`MealIngredient` (models), `active_goals` (goals repo), `remaining`/`assess_day`/`FitAssessment` (budget), the `meals`/`meal_ingredients` tables.
- Produces:
  - `log_verified_meal(conn, *, name, date, ingredients, status='eaten', confidence='high', assumptions='') -> int` — ADD the `confidence` and `assumptions` parameters; write them into the existing `confidence`/`assumptions` columns instead of the current hardcoded `'high'`/`''`. Defaults preserve today's behavior (existing callers/tests unaffected). All else unchanged (engine-computed totals, per-ingredient snapshots, one commit).
  - `day_status(conn, date) -> dict` with keys: `target` (`Macros | None` from `active_goals`), `verified` (`Macros` — exact sum of non-estimated meals that day), `estimated` (`Macros` — exact sum of estimated meals that day), `consumed` (`verified + estimated`), `remaining` (`Macros | None` = `remaining(target, consumed)` or None if no target), `assessment` (`FitAssessment | None` = `assess_day(remaining)` or None). Per-meal sums reuse `day_totals`' rule (sum exact `meal_ingredients` rows when present, else the legacy meal columns); bucket each meal by `status == 'estimated'`.

- [ ] **Step 1: Write failing tests** `tests/test_nutrition_store_day_status.py` (class-based, `connect(":memory:")`):
  - `log_verified_meal(..., confidence='low', assumptions='eyeballed')` persists those values (read the `meals` row back); the default call still writes `'high'`/`''`.
  - `day_status` with a target + one verified meal + one estimated meal → `verified`/`estimated` buckets each hold the right exact macros, `consumed == verified + estimated`, `remaining == target - consumed`, `assessment` is a `FitAssessment`.
  - `day_status` with NO active goal → `target`/`remaining`/`assessment` are None, buckets still populated (Review-Focus 4).
  - `day_status` for an over-budget day → `assessment.status == 'over'` (Review-Focus 5).
  - A legacy estimate row (no ingredient rows, `status='estimated'`) lands in the `estimated` bucket.
- [ ] **Step 2: Run** `python -m pytest tests/test_nutrition_store_day_status.py -v` → FAIL.
- [ ] **Step 3: Implement** the two changes in `meals.py`. Factor the per-day summation so `day_totals` and `day_status` share the "exact-ingredients-else-legacy" rule rather than duplicating the SQL.
- [ ] **Step 4: Run** the new test + regression `python -m pytest tests/test_nutrition_store_meals.py -q` (existing `log_verified_meal`/`day_totals` tests must still pass).
- [ ] **Step 5: Commit** — `feat(nutrition): flaggable meal logging + day_status (verified/estimated split)`.

---

### Task 2: Pantry depletion primitive — epsilon floor + `PantryStore.deplete`

**Files:**
- Modify: `nutrition_core/store/pantry.py` (`adjust_quantity` epsilon floor)
- Modify: `jarvisd/pantry_store.py` (add `deplete`)
- Test: `tests/test_pantry_deplete.py`

**Interfaces:**
- Consumes: Plan 2's `adjust_quantity`, `find_by_food` (pantry repo); `PantryStore` (Plan 2).
- Produces:
  - `adjust_quantity` (pantry repo): change the empty check so a computed remaining `<= 1e-9` deletes the row (was `== 0.0`). This stops repeated fractional depletions from leaving a ~1e-14 g ghost row. Positive-delta and exact behavior otherwise unchanged.
  - `PantryStore.deplete(self, *, source, external_id, grams) -> float | None` — `find_by_food(source, external_id)`; if found, `adjust_quantity(row_id, -grams)` and return the remaining grams; if not in the pantry, return `None` (nothing to deplete). `PantryStore.deplete_by_id(self, pantry_id, grams) -> float` — `adjust_quantity(pantry_id, -grams)` directly (for ingredients chosen by pantry id). These are the handles the verified log calls.

- [ ] **Step 1: Write failing tests** `tests/test_pantry_deplete.py` (class-based, in-memory `PantryStore`):
  - repeated fractional depletions that should empty a row actually DELETE it (no sub-nanogram ghost) — e.g. deplete 0.1 g thirty times from 3.0 g → row gone (Review-Focus 3 precision).
  - `deplete(source, external_id, grams)` on a tracked item → remaining reduced; over-deplete (grams > tracked) → row removed, returns 0.0, never negative; `deplete` of a food NOT in pantry → returns None, no error.
  - `deplete_by_id` reduces/removes the given row.
- [ ] **Step 2: Run** `python -m pytest tests/test_pantry_deplete.py -v` → FAIL.
- [ ] **Step 3: Implement** the epsilon floor in `adjust_quantity` and the two `PantryStore` methods.
- [ ] **Step 4: Run** the new test + regression `python -m pytest tests/test_nutrition_store_pantry.py tests/test_tools_pantry.py -q` (Plan 2 behavior intact).
- [ ] **Step 5: Commit** — `feat(macro-chef): pantry depletion handles + epsilon floor on adjust_quantity`.

---

### Task 3: Ingredient resolver + `log_meal` (verified, with depletion)

**Files:**
- Create: `jarvisd/tools/meal_log.py` (the resolver + `log_meal`; `log_estimated_meal`/`day_status` added in Task 4)
- Test: `tests/test_tools_meal_log.py`

**Interfaces:**
- Consumes: `FoodService.by_barcode`/`lookup` (Plans 1–2), `PantryStore.list_items`/`deplete`/`deplete_by_id` (Task 2), `log_verified_meal` (Task 1), `MealIngredient`/`Macros`/`FoodSource` (models).
- Produces:
  - `ResolvedIngredient` (frozen dataclass): `meal_ingredient: MealIngredient`, `source: str`, `external_id: str | None`, `pantry_id: int | None` — carries everything the log needs both to compute (via `meal_ingredient`) and to deplete (via `pantry_id`/`source`+`external_id`).
  - `resolve_ingredient(spec, *, food_service, pantry_store) -> ResolvedIngredient | str` — `spec` is a dict `{grams, and exactly one of: barcode | name | pantry_id}`. On success returns a `ResolvedIngredient`; on any failure returns an **error string** naming the problem. Resolution: validate `grams` is a finite number > 0 (reject bool/nan/inf/≤0); `pantry_id` → look up that row in `list_items`, use its stored snapshot (`per100_*`, `source`, `name`), set `pantry_id`, error if absent; `barcode` → `food_service.by_barcode`, set `source`/`external_id` from the `FoodItem`, error on miss / no food_service; `name` → `food_service.lookup` first match, set `source`/`external_id`, error on empty / no food_service.
  - `TOOLS` (so far `[log_meal]`) + `handlers(conn, food_service, pantry_store)` — see the dep note below.
  - `log_meal` tool input: `name` (string, required), `ingredients` (array, required, ≥1) each `{grams:number}` + one of `barcode`/`name`/`pantry_id`. It resolves EVERY ingredient first; if ANY returns an error string, it logs nothing and returns that error (Review-Focus 1). Otherwise it calls `log_verified_meal(conn, name=, date=today, ingredients=[r.meal_ingredient for r in resolved], status='verified', confidence='high')`, then **best-effort** depletes each resolved ingredient: if `r.pantry_id is not None` → `pantry_store.deplete_by_id(r.pantry_id, grams)`; elif `r.external_id is not None` → `pantry_store.deplete(source=r.source, external_id=r.external_id, grams=grams)` (a no-op returning None when the food isn't in the pantry). A depletion exception is caught and noted, never un-logs (Review-Focus 2). Returns a summary: dish name, computed totals (from the repo), and what was depleted.
- **Dep note:** the resolver/log needs the raw `conn` (for `log_verified_meal`) plus `food_service` + `pantry_store`. The handlers close over all three: `handlers(conn, food_service, pantry_store)`, where `conn` is the same connection `PantryStore`/`MealStore` hold (Task 5 passes the shared `conn` from bootstrap).

- [ ] **Step 1: Write failing tests** `tests/test_tools_meal_log.py` (class-based; real in-memory DB + `PantryStore`, fake `food_service` with stub `by_barcode`/`lookup` incl. a miss and a raising variant):
  - verified log from a name ingredient → a `meals` row with `status='verified'` and exact engine totals (assert via `day_status`/`day_totals`).
  - a `pantry_id` ingredient → logs AND depletes that pantry row by the grams (assert pantry grams dropped).
  - a barcode ingredient that matches a pantry item → logs and depletes via `find_by_food`.
  - **one ingredient fails to resolve** (barcode miss / unknown pantry_id / grams=0) → nothing logged (day totals unchanged), nothing depleted, error names the ingredient (Review-Focus 1).
  - **depletion raises** (monkeypatch `pantry_store.deplete` to throw) → meal STILL logged, returns success-with-warning (Review-Focus 2).
  - **over-deplete** (cook 200 g, pantry has 150 g) → logged, pantry row removed, no negative (Review-Focus 3 end-to-end).
  - bad top-level input (missing `name`, empty `ingredients`, non-list) → error string, nothing logged; tool never raises.
- [ ] **Step 2: Run** `python -m pytest tests/test_tools_meal_log.py -v` → FAIL.
- [ ] **Step 3: Implement** `resolve_ingredient` + `log_meal` in `jarvisd/tools/meal_log.py`. Resolve-all-then-log-then-deplete ordering is load-bearing (Review-Focus 1 & 2). Reuse pantry's number-validation idea (reject bool/nan/inf/≤0).
- [ ] **Step 4: Run** → PASS; regression `python -m pytest tests/ -k "pantry or meal or food" -q`.
- [ ] **Step 5: Commit** — `feat(macro-chef): DB-sourced log_meal with pantry depletion`.

---

### Task 4: `log_estimated_meal` + `day_status` tools

**Files:**
- Modify: `jarvisd/tools/meal_log.py` (add two tools + handlers)
- Test: `tests/test_tools_meal_log_estimated.py`, `tests/test_tools_day_status.py`

**Interfaces:**
- Consumes: `resolve_ingredient` (Task 3), `log_verified_meal` (Task 1, used with `status='estimated'`), `day_status` repo (Task 1), `assess_day`/`FitAssessment` (via the repo result).
- Produces (added to `TOOLS` + `handlers`):
  - `log_estimated_meal` tool input: `description` (string, required — stored as the meal name), `ingredients` (array ≥1, same shape as `log_meal` but intended as name+grams eyeballed), optional `confidence` (`low`/`medium`, default `low`). Resolves every ingredient via `resolve_ingredient` (same reject-on-any-failure rule); logs via `log_verified_meal(conn, name=description, date=today, ingredients=[...], status='estimated', confidence=<conf>, assumptions='estimated portions')`. **Never depletes the pantry.** Returns a summary that states the macros ARE estimates.
  - `day_status` tool input: none (optional `date` string; default today). Calls the `day_status` repo function and formats a MacroFactor-style status: target (or "no daily target set" — Review-Focus 4), consumed with the **verified/estimated split shown explicitly** (e.g. "1,650 kcal (1,400 verified + 250 estimated)" — Review-Focus 5), remaining, and an honest over/cornered line when `assessment.status != 'ok'`.
- Both handlers never raise.

- [ ] **Step 1: Write failing tests**:
  - `tests/test_tools_meal_log_estimated.py`: estimated log → `meals` row with `status='estimated'`, engine-computed totals from DB per-100g × given grams, confidence stored, pantry UNCHANGED (no depletion even when an ingredient matches a pantry item); one bad ingredient → nothing logged; never raises.
  - `tests/test_tools_day_status.py`: with target + mixed verified/estimated meals → the formatted string shows target, the split, and remaining; with no target → "no daily target set", no crash (Review-Focus 4); over-budget day → the over line appears (Review-Focus 5); empty day → a clean "nothing logged yet" style message; never raises.
- [ ] **Step 2: Run** both test files → FAIL.
- [ ] **Step 3: Implement** the two tools in `meal_log.py`.
- [ ] **Step 4: Run** → PASS; regression `python -m pytest tests/ -k "meal_log or day_status" -q`.
- [ ] **Step 5: Commit** — `feat(macro-chef): log_estimated_meal + day_status tools`.

---

### Task 5: Wiring — register meal_log, retire the LLM log_meal

**Files:**
- Modify: `jarvisd/tools/__init__.py` (`build_registry` gains `meal_log_conn=None`; register `meal_log.TOOLS`/handlers when `meal_log_conn` + `pantry_store` + `food_service` are present)
- Modify: `jarvisd/core.py` (`Assistant.__init__` forwards the new dep)
- Modify: `jarvisd/bootstrap.py` (pass the existing `conn` for meal_log)
- Modify: `jarvisd/tools/meals.py` (REMOVE the legacy LLM-estimated `log_meal` tool def + its handler — keep `meal_summary`/`meal_trends`/`export_meals`/`set_nutrition_targets`)
- Test: `tests/test_tools_registry_meal_log.py`

**Interfaces:**
- Produces: the DB-sourced `log_meal`/`log_estimated_meal`/`day_status` tools wired into a running assistant; the old estimated `log_meal` gone (no duplicate tool name); all new params default `None`.

- [ ] **Step 1: Write failing test** `tests/test_tools_registry_meal_log.py`: `build_registry(..., food_service=<fake>, pantry_store=<fake>, meal_log_conn=<conn>)` → `log_meal`/`log_estimated_meal`/`day_status` present in tools + handlers, AND the handler map's `log_meal` is the new one (e.g. its tool def requires `ingredients`, not `calories`); default build → these absent; registering meal_log does not drop pantry/food tools (non-regression). Also assert the legacy estimated `log_meal` (with `calories` required) is no longer produced by `meals.TOOLS`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement**: remove the legacy `log_meal` from `meals.py` TOOLS + handlers dict (leave the other four tools and `MealStore` untouched). Thread `meal_log_conn` through `build_registry` → `Assistant.__init__` → from `bootstrap` (reuse the existing `conn`), registering the meal_log tools when `meal_log_conn`, `pantry_store`, and `food_service` are all present. Import `meal_log` in the registry module. Mirror the food/pantry wiring exactly (all new params default None; pantry/food blocks untouched).
- [ ] **Step 4: Run** the registry test → PASS; whole suite `python -m pytest tests/ -q` (expect only the 3 pre-existing port-8765 server failures; any test asserting the old `log_meal` estimated behavior must be updated to the new tool as part of this task).
- [ ] **Step 5: Commit** — `feat(macro-chef): wire DB-sourced meal logging into JARVIS, retire LLM log_meal`.

---

## Done when

JARVIS logs meals from DB/pantry ingredients with engine-computed macros (never LLM-invented), cooking a meal auto-depletes the pantry (best-effort, never negative), eating out logs as honestly-flagged estimates that don't touch the pantry, and `day_status` shows a MacroFactor-grade daily picture with the verified/estimated split against the target. The old LLM-estimated `log_meal` is gone; all new wiring params default `None`; full suite green but for the pre-existing port-8765 server tests.

**Next:** Plan 4 — Suggestions (curated sweet/savory/generic dishes from the pantry that fit remaining macros, propose-then-`check_dish`-validate; logging a chosen suggestion reuses this plan's `log_meal`). **Before Plan 4 trusts the numbers:** resolve the parked Plan-1 confidence item (present-but-unparseable adapter macro → 0.0; distinguish absent vs unparseable + range sanity, both adapters) and the "partial outage reads as no-matches" nuance. Then Plan 5 — Meal-prep + reminders.
