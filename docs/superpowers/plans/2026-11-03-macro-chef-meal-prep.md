# Macro Chef — Meal-Prep + Reminders Implementation Plan (Plan 5 of 5)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the user lock specific dishes for the coming day(s) as a meal-prep plan — the dishes are stored as deterministic, engine-computed `planned` meals that do NOT touch today's budget until actually cooked; the system computes an aggregate shopping delta (what the plan needs minus what the pantry holds) and sets day-ahead prep + shopping reminders on JARVIS's existing reminder system; and cooking a planned meal transitions it to `verified` and depletes the pantry at that moment.

**Architecture:** A new `planned` meal status (alongside `verified`/`estimated`) that is excluded from consumed totals. A new `jarvisd/tools/prep.py` with `plan_prep` (resolve + validate each locked dish via Plan 3's `resolve_ingredient` and Plan 4's `validate_dish`, store each as a `planned` meal for its date, compute the shopping delta, set reminders), `cook_meal` (transition a planned meal → verified + deplete pantry), and `list_planned_meals` (upcoming locked meals, for cooking reference). Because depletion happens at cook time — possibly days after planning — each planned meal's ingredients persist their pantry/food provenance (`external_id`, `pantry_id`), added to `meal_ingredients` by a migration. Reminders reuse `ReminderStore`; no parallel reminder system.

**Tech Stack:** Python 3.11, existing `nutrition_core` (engine/budget/models/store), `FoodService` (Plan 1), `PantryStore` (Plan 2), `resolve_ingredient`/`log_verified_meal`/meals repo (Plan 3), `validate_dish` (Plan 4), `ReminderStore` (existing), JARVIS tool-registry pattern, SQLite. No new dependency.

**Spec:** `docs/superpowers/specs/2026-10-31-macro-chef-tool-pack-design.md` (§4.5 meal-prep, §4.1/4.9 tool surface, §8 resolutions 2 & 3). Builds on Plans 1–4, all merged to master.

## Global Constraints

- **Python 3.11.** No new dependency.
- **Zero-error (binding):** a `planned` meal's macros are engine-computed from resolved per-100g snapshots (reusing `resolve_ingredient`/`validate_dish`/`log_verified_meal`), never caller-supplied. A dish with any unresolvable ingredient is NEVER locked as a planned meal.
- **`planned` never eats today's budget:** `day_totals`, `day_status`, and `remaining_today` count only consumed statuses (`verified`, `estimated`, legacy `eaten`) and EXCLUDE `planned`. A locked meal affects the budget only once cooked.
- **Depletion only at cook time:** planning never changes pantry grams. Cooking a planned meal depletes each ingredient's exact grams, best-effort, exactly once (no double-deplete on re-cook).
- **Reminders reuse `ReminderStore`** (`jarvisd/reminder_store.py`): `add(text, due_ts, recurrence='none', condition=None) -> int`, `due_ts` ISO. Do NOT build a parallel reminder table/scheduler.
- **Migration DDL is owned by `nutrition_core`** (`nutrition_core/store/schema.py`), imported by `jarvisd/db.py` as the next migration with `SCHEMA_VERSION` bumped (currently 8 → 9); additive + idempotent-safe on a live upgrade.
- **Tools never raise** — each returns a human-readable string including on every bad input (follow `jarvisd/tools/meal_log.py`/`suggest.py`). New `build_registry`/`Assistant` params default `None` (non-regression); reuse the existing `meal_log_conn`+`food_service`+`pantry_store` trio gate.
- **No live network in tests** — fake `FoodService`; real in-memory DB via `from jarvisd.db import connect; connect(":memory:")`.
- Commits end: `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.

## Review Focus

1. **`planned` excluded from the budget** — a planned meal dated TODAY still contributes 0 to `consumed`/`remaining`; `day_status`/`day_totals`/`remaining_today` are unchanged by locking a plan. Only cooking it moves it into consumed. *(Task 1.)*
2. **Cook depletes exactly once; stale/short/missing linkage** — cooking transitions `planned`→`verified` and depletes; cooking an already-cooked or non-planned meal is a safe no-op (no second depletion); if a planned ingredient's pantry row is gone or short at cook time, deplete what's there and report it (never crash, never go negative). *(Tasks 1 & 4.)*
3. **Shopping delta correctness** — the to-buy list = aggregate grams needed across ALL locked dishes minus current pantry `grams_available`: an ingredient fully covered → absent; partially covered → only the shortfall grams; the same ingredient across two dishes → summed once; an ingredient not in the pantry → its full need. *(Task 2.)*
4. **Unresolvable ingredient in a prep dish** — reported, and that dish is NOT locked (no planned meal with guessed numbers); other valid dishes in the same call are still locked. *(Task 2.)*
5. **Reminder timing + degeneracy** — the day-ahead reminder's `due_ts` = planned date − 1 day; planning for tomorrow sets it for today; planning for today (day-ahead already past) clamps to now rather than inserting a past/negative reminder or crashing; no shopping reminder is set when nothing needs buying. *(Task 3.)*

---

### Task 1: `planned` status — repo + migration + cook transition

**Files:**
- Modify: `nutrition_core/store/schema.py` (migration-9 DDL const) + `jarvisd/db.py` (register migration 9, `SCHEMA_VERSION = 9`)
- Modify: `nutrition_core/store/meals.py`
- Test: `tests/test_nutrition_store_planned.py`

**Interfaces:**
- Consumes: `log_verified_meal`, `_day_meal_totals`, `day_totals`, `day_status`, `remaining_today` (existing, this file); `DbConnection`.
- Produces:
  - Migration 9 (DDL in `schema.py`, e.g. `MEAL_INGREDIENT_LINK_SQL`): `ALTER TABLE meal_ingredients ADD COLUMN external_id TEXT;` and `ALTER TABLE meal_ingredients ADD COLUMN pantry_id INTEGER;` (both nullable; existing rows get NULL). `jarvisd/db.py` adds `9: MEAL_INGREDIENT_LINK_SQL` and bumps `SCHEMA_VERSION` to 9.
  - `CONSUMED_STATUSES: frozenset[str]` = `{"verified", "estimated", "eaten"}` (everything a day actually ate; `planned` is deliberately absent).
  - `log_verified_meal(...)` gains `links: Sequence[tuple[str | None, int | None]] | None = None` — per-ingredient `(external_id, pantry_id)` aligned by index with `ingredients`; when `None`, every ingredient stores NULL/NULL (preserves all existing callers). Stored into the new `meal_ingredients.external_id`/`pantry_id` columns.
  - `day_totals` and `day_status` count a meal only when `status in CONSUMED_STATUSES` (so `planned` is skipped); `remaining_today` inherits this via `day_totals`. (`_day_meal_totals` keeps returning all rows with their status; the filtering is in these three consumers.)
  - `cook_planned_meal(conn, meal_id: int) -> dict`: if the meal is not `status='planned'`, return `{"ok": False, "reason": <str>}` and change nothing. Else UPDATE it to `status='verified'` in place, read its ingredient rows, commit, and return `{"ok": True, "name": str, "date": str, "ingredients": [{"name","grams","source","external_id","pantry_id"}, ...]}`. One transaction. Does NOT deplete pantry (the tool layer does).
  - `list_planned(conn, *, from_date: str | None = None) -> list[dict]`: planned meals (`status='planned'`), `ts >= from_date` when given, ordered by `ts, id`; each `{"id","name","date","macros": Macros}` (macros = that meal's exact ingredient total via the existing per-meal summing).

- [ ] **Step 1: Write failing tests** `tests/test_nutrition_store_planned.py` (in-memory DB): a `planned` meal dated today contributes 0 to `day_totals`/`day_status["consumed"]`/`remaining_today` while a `verified` meal the same day counts (Review-Focus 1); `log_verified_meal(..., links=[("off123", None), (None, 7)])` round-trips into `meal_ingredients.external_id`/`pantry_id`; `cook_planned_meal` on a planned meal flips it to `verified` (now counts in `day_totals`) and returns its ingredients with linkage; `cook_planned_meal` on an already-`verified`/absent meal returns `ok=False` and leaves status unchanged (Review-Focus 2); `list_planned` returns only planned meals, filtered by `from_date`.
- [ ] **Step 2: Run** `python -m pytest tests/test_nutrition_store_planned.py -v` → FAIL.
- [ ] **Step 3: Implement** the migration (schema.py const + db.py registration + `SCHEMA_VERSION=9`), `CONSUMED_STATUSES`, the `links` param, the three consumers' status filter, `cook_planned_meal`, `list_planned`.
- [ ] **Step 4: Run** the new test + regression `python -m pytest tests/test_nutrition_store_meals.py tests/test_nutrition_store_day_status.py tests/test_tools_meal_log.py tests/test_tools_meal_log_estimated.py -q` (existing day-total/status tests must still pass — none use `planned`).
- [ ] **Step 5: Commit** — `feat(nutrition): planned meal status + ingredient provenance + cook transition`.

---

### Task 2: `plan_prep` — lock dishes + shopping delta

**Files:**
- Create: `jarvisd/tools/prep.py`
- Test: `tests/test_tools_plan_prep.py`

**Interfaces:**
- Consumes: `resolve_ingredient`/`ResolvedIngredient` (meal_log), `validate_dish` (suggest, for the computed macros + fit note — optional to surface), `log_verified_meal` + `CONSUMED_STATUSES` (meals repo), `PantryStore.list_items`/`find_by_food`, `Macros`.
- Produces (first half of the module):
  - `plan_prep` tool: input `dishes` — array (1+) of `{name: str, date: str (ISO yyyy-mm-dd), ingredients: [...]}` where `ingredients` is the `log_meal` shape (each `{grams}` + exactly one of `barcode`/`name`/`pantry_id`). For each dish: resolve ALL ingredients; if any fails, record a per-dish error and SKIP that dish (do NOT lock it) — other dishes still proceed (Review-Focus 4). For a fully-resolved dish, persist it as a `planned` meal via `log_verified_meal(conn, name=..., date=<dish date>, ingredients=<resolved MealIngredients>, status='planned', links=<(external_id, pantry_id) per ingredient>)`. **Never depletes pantry.**
  - `_shopping_delta(resolved_dishes, pantry_store) -> list[dict]`: aggregate grams NEEDED per food key across ALL locked dishes — key = `pantry_id` when set, else `(source, external_id)`, else the lowercased name — then subtract the matching current pantry row's `grams_available` (via `list_items`/`find_by_food`); emit `{"name","need_g","have_g","buy_g"}` only where `buy_g = max(0, need − have) > 0`. Same food across dishes sums once (Review-Focus 3).
  - `plan_prep` returns a readable string: the locked dishes (name, date, computed macros), any skipped dishes with the reason, and the shopping list (or "nothing to buy — your pantry covers this plan"). Never raises; bad input (empty `dishes`, bad date, missing fields, non-list) → error string.
  - Module exports `TOOLS = [plan_prep]` and `handlers(conn, food_service, pantry_store)` (extended in Tasks 3–4).

- [ ] **Step 1: Write failing tests** `tests/test_tools_plan_prep.py` (in-memory DB, seeded PantryStore, fake food_service): locking 2 dishes for tomorrow creates 2 `planned` meals (via `list_planned`) and does NOT change `remaining_today`/pantry grams; the shopping delta omits a fully-stocked ingredient, lists only the shortfall grams for a partially-stocked one, sums an ingredient used in two dishes once, and lists the full need for an ingredient absent from the pantry (Review-Focus 3); a dish with an unresolvable ingredient is reported and NOT locked while a valid dish in the same call IS locked (Review-Focus 4); bad input → error string, never raises.
- [ ] **Step 2: Run** `python -m pytest tests/test_tools_plan_prep.py -v` → FAIL.
- [ ] **Step 3: Implement** `prep.py` (`plan_prep`, `_shopping_delta`, `TOOLS`, `handlers`). Reuse `resolve_ingredient`; do not reimplement resolution or arithmetic.
- [ ] **Step 4: Run** → PASS; regression `python -m pytest tests/ -k "prep or meal_log or suggest or planned" -q`.
- [ ] **Step 5: Commit** — `feat(macro-chef): plan_prep locks dishes as planned meals + shopping delta`.

---

### Task 3: `plan_prep` reminders (day-ahead)

**Files:**
- Modify: `jarvisd/tools/prep.py`
- Test: `tests/test_tools_plan_prep_reminders.py`

**Interfaces:**
- Consumes: `ReminderStore` (`from jarvisd.reminder_store import ReminderStore`) built on the same `conn` (`ReminderStore(conn)`); the locked dishes + shopping delta from Task 2.
- Produces: `plan_prep`, after locking + delta, sets reminders via `ReminderStore(conn).add(text, due_ts, recurrence="none")`:
  - One **prep reminder per distinct planned date**: `text` names the dishes for that date; `due_ts` = that date − 1 day at a fixed local hint time (e.g. `T18:00:00`). Helper `_day_before(date_iso: str, *, now: datetime) -> str` returns the ISO `due_ts`, and if that instant is already in the past (planning for today/past) it clamps to `now` (fire-now) rather than emitting a past-dated reminder (Review-Focus 5).
  - One **shopping reminder** when the shopping list is non-empty: `text` lists the to-buy items; `due_ts` = earliest planned date − 1 day (same clamp). No shopping reminder when nothing needs buying.
  - `plan_prep`'s returned string now also states what reminders were set. Reminder failures are caught and reported, never raised (the plan is already stored).

- [ ] **Step 1: Write failing tests** `tests/test_tools_plan_prep_reminders.py`: planning dishes for a date N≥2 days out sets a prep reminder whose `due_ts` is exactly one day before that date and whose text names the dishes (assert against `ReminderStore(conn).list_active()`); a non-empty shopping delta sets one shopping reminder with the earliest-date-minus-one `due_ts`; an empty delta sets NO shopping reminder; planning for today → the prep reminder `due_ts` is clamped to ~now (not in the past), no crash (Review-Focus 5).
- [ ] **Step 2: Run** `python -m pytest tests/test_tools_plan_prep_reminders.py -v` → FAIL.
- [ ] **Step 3: Implement** the reminder step + `_day_before` clamp in `prep.py`.
- [ ] **Step 4: Run** → PASS; regression `python -m pytest tests/ -k "prep or reminder" -q`.
- [ ] **Step 5: Commit** — `feat(macro-chef): day-ahead prep + shopping reminders for plan_prep`.

---

### Task 4: `cook_meal` + `list_planned_meals` tools

**Files:**
- Modify: `jarvisd/tools/prep.py`
- Test: `tests/test_tools_cook_meal.py`

**Interfaces:**
- Consumes: `cook_planned_meal`, `list_planned` (meals repo, Task 1); `PantryStore.deplete_by_id`/`deplete` (Plan 3); `day_totals` (to show the budget now moved).
- Produces (added to `TOOLS` + `handlers`):
  - `list_planned_meals` tool: input optional `from_date` (ISO; default today). Returns the upcoming planned meals from `list_planned` — each with its id, name, date, and computed macros — so the user/LLM can reference one to cook. Never raises.
  - `cook_meal` tool: input `meal_id` (integer, required). Calls `cook_planned_meal(conn, meal_id)`. If `ok=False` → return its reason (e.g. "that meal isn't a locked plan" / "already cooked"), nothing depleted. If `ok=True` → for EACH returned ingredient deplete best-effort: `pantry_store.deplete_by_id(pantry_id, grams)` when `pantry_id` is set and the row still exists, else `pantry_store.deplete(source=source, external_id=external_id, grams=grams)` when `external_id` is set, else record "not in pantry / couldn't match". Report what was depleted and any shortfalls/misses; state the meal is now logged (verified) and counts toward today. Never double-deplete (the status already flipped; re-calling finds it non-planned). Never raises.
  - `TOOLS` now `[plan_prep, cook_meal, list_planned_meals]`.

- [ ] **Step 1: Write failing tests** `tests/test_tools_cook_meal.py` (in-memory DB, seeded pantry, fake food_service): lock a dish via `plan_prep` then `cook_meal(meal_id)` → the meal is now `verified` (counts in `day_totals`) AND pantry grams dropped by exactly the cooked grams (via `pantry_id` link and via `source`/`external_id` link) (Review-Focus 2); `cook_meal` on the same id again → safe no-op message, pantry UNCHANGED the second time (no double-deplete) (Review-Focus 2); `cook_meal` on a planned ingredient whose pantry row was removed → cooks, reports it couldn't deplete that item, does not crash or go negative; `cook_meal` on a bad/absent id → error string; `list_planned_meals` lists locked meals and omits cooked ones.
- [ ] **Step 2: Run** `python -m pytest tests/test_tools_cook_meal.py -v` → FAIL.
- [ ] **Step 3: Implement** `cook_meal` + `list_planned_meals` in `prep.py`.
- [ ] **Step 4: Run** → PASS; regression `python -m pytest tests/ -k "prep or cook or planned or pantry" -q`.
- [ ] **Step 5: Commit** — `feat(macro-chef): cook_meal (planned→verified + deplete) + list_planned_meals`.

---

### Task 5: Wiring

**Files:**
- Modify: `jarvisd/tools/__init__.py` (register `prep.TOOLS`/handlers)
- Test: `tests/test_tools_registry_prep.py`

**Interfaces:**
- Consumes: `prep.TOOLS` + `prep.handlers(conn, food_service, pantry_store)`.
- Produces: `plan_prep`/`cook_meal`/`list_planned_meals` registered whenever the existing trio is present — reuse the SAME gate as meal_log/suggest (`meal_log_conn is not None and pantry_store is not None and food_service is not None`), calling `prep.handlers(meal_log_conn, food_service, pantry_store)`. No new `build_registry`/`Assistant`/`bootstrap` parameter (the trio and the shared conn — which `ReminderStore` also uses — are already threaded). Do NOT disturb the food/pantry/meal_log/suggest registrations.

- [ ] **Step 1: Write failing test** `tests/test_tools_registry_prep.py` (mirror `test_tools_registry_suggest.py`): with the trio present → `plan_prep`, `cook_meal`, `list_planned_meals` in the registry's tools AND handlers; default/partial build → none present; registering prep does not drop the food/pantry/meal_log/suggest tools.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** the registration (import `prep`; register under the shared gate).
- [ ] **Step 4: Run** the registry test → PASS; whole suite `python -m pytest tests/ -q` (expect green, modulo any environmental port-8765 server tests). Report exact counts.
- [ ] **Step 5: Commit** — `feat(macro-chef): wire meal-prep tools into JARVIS`.

---

## Done when

The user can lock dishes for the coming day(s): each is stored as a deterministic `planned` meal that doesn't touch today's budget, the system reports an aggregate shopping delta (plan minus pantry) and sets day-ahead prep + shopping reminders on JARVIS's reminder system, and cooking a planned meal (`cook_meal`) transitions it to `verified` and depletes the pantry exactly once with the stored provenance. Unresolvable prep dishes are never locked; `planned` meals are excluded from `day_status`/`day_totals`/`remaining_today` until cooked. All new wiring reuses the existing trio gate with no new params; full suite green but for any environmental port-8765 server tests.

**This is the last plan of the JARVIS tool-pack (face 2).** After it, remaining Macro Chef scope is tracked in project memory: the partial-outage surfacing nuance, the Plan-3 minors (per-macro `day_status` split, date validation, an end-to-end `Assistant(meal_log_conn=)` test), the Plan-4 display cosmetics, and the separate PWA face (FastAPI Engine Service + Next.js/Vercel, MacroFactor-grade polish) with its libSQL/Turso embedded replica (+ `rollback()` on the `DbConnection` protocol).
