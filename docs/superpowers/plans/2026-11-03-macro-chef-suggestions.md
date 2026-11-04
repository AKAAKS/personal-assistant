# Macro Chef — Suggestions Implementation Plan (Plan 4 of 5)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let JARVIS answer "what should I eat?" with a small curated set of dishes — one sweet, one savory, one generic breakfast — built from what's actually in the pantry and sized to fit the day's remaining macros, where **JARVIS proposes the dishes but a deterministic validator computes every number and decides the fit** (propose-then-code-compute; no LLM-invented macros, no nested LLM call). Logging a chosen suggestion reuses Plan 3's `log_meal`.

**Architecture:** The main JARVIS loop reads the day's remaining budget (`day_status`) and the pantry (`pantry_list`), proposes candidate dishes (name + slot + ingredients with grams, drawn from pantry items), and passes them to a deterministic validator. `validate_dish` reuses Plan 3's `resolve_ingredient` to turn each ingredient into a real per-100g snapshot, computes the dish total with the engine, checks the pantry has enough grams, and assesses fit against remaining macros via `budget.assess_day`. `check_dish` validates one dish; `suggest_meals` validates a batch and returns the best-fitting dish per slot (or an honest "nothing fits — create your own"). Two prerequisites ship first: adapter confidence hardening (a present-but-unparseable or out-of-range macro drops the food instead of becoming a silent 0.0) and the carried Plan-3 cleanups.

**Tech Stack:** Python 3.11, existing `nutrition_core` (engine/budget/models/adapters/store), `FoodService` (Plan 1), `PantryStore` (Plan 2), `resolve_ingredient`/`meal_log` (Plan 3), JARVIS tool-registry pattern, SQLite. No new dependency.

**Spec:** `docs/superpowers/specs/2026-10-31-macro-chef-tool-pack-design.md` (curated suggestions, pantry-constrained, propose-then-check, no haggling). Builds on Plans 1–3, all merged.

## Global Constraints

- **Python 3.11.** No new dependency.
- **Zero-error (binding):** no suggested macro is LLM-supplied. The LLM proposes dish names + ingredient names/grams/ids; `validate_dish` computes every macro via `nutrition_core.engine` from resolved per-100g snapshots (reusing `resolve_ingredient`), or the dish is reported unvalidatable. A dish whose ingredient can't resolve is NEVER presented with numbers.
- **Pantry-constrained:** suggested dishes are built from pantry items; `validate_dish` checks the pantry actually holds enough grams of each pantry-sourced ingredient and reports shortfalls. A dish the pantry can't supply is not offered as ready-to-cook.
- **Fit is computed, not claimed:** fit against the day's remaining macros comes from `budget.assess_day(remaining − dish_total)` (`ok`/`over`/`cornered`), never from the LLM's judgment.
- **No haggling:** `suggest_meals` returns at most one dish per slot (sweet / savory / breakfast), already validated with real numbers; the user's only choice is cook-this or ask-for-a-different-taste. No back-and-forth portion tuning.
- **Tools never raise** — each returns a human-readable string, including on every bad input (follow `jarvisd/tools/meal_log.py`/`pantry.py`). New `build_registry`/`Assistant` params default `None` (non-regression).
- **No live network in tests** — fake `FoodService`; real in-memory DB via `from jarvisd.db import connect; connect(":memory:")`.
- Commits end: `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.

## Review Focus

1. **Unparseable/out-of-range adapter macro drops the food** — a food whose protein/carbs/fat field is PRESENT but non-numeric, negative, or physically impossible (>100 g per 100 g; kcal absurd) is dropped from search results, not kept with a silent 0.0; an ABSENT field still defaults to 0.0. *(Task 1.)*
2. **A dish with one unresolvable ingredient is never presented with numbers** — `validate_dish` reports it as unvalidatable; `suggest_meals` drops it from the set rather than showing a partial/guessed total. *(Task 3.)*
3. **Pantry shortfall is caught** — a proposed dish needing 200 g of an item with 150 g in the pantry is flagged (not offered as ready-to-cook, or clearly marked short). *(Task 3.)*
4. **No target set / nothing fits** — `check_dish`/`suggest_meals` with no active goal still compute macros but report fit as unknown (no crash); when every candidate is over/cornered/short, `suggest_meals` returns a clean "nothing fits — create your own" message, not an empty or misleading reply. *(Tasks 3 & 4.)*
5. **Suggestions never auto-log or auto-deplete** — `check_dish`/`suggest_meals` are read-only previews: they must not write a meal row or touch pantry grams. Logging happens only when the user later calls `log_meal`. *(Tasks 3 & 4.)*

---

### Task 1: Adapter confidence hardening (parked Plan-1 item)

**Files:**
- Modify: `nutrition_core/adapters/base.py` and/or `nutrition_core/adapters/usda.py` + `nutrition_core/adapters/openfoodfacts.py`
- Test: `tests/test_adapter_confidence.py`

**Interfaces:**
- Consumes: the existing adapter parsing (`macros_from_fields` in base.py; `_number`/`_nutrient_values` in the adapters), `Macros`.
- Produces: a food is DROPPED from `search`/`get_by_barcode` results when any macro field is **present but untrustworthy** — i.e. present-but-unparseable (non-numeric/`None`/non-finite), negative, or out of a sane range (protein/carbs/fat > 100 g per 100 g; kcal < 0 or implausibly high, e.g. > 1000 kcal/100 g). A field that is simply ABSENT keeps its 0.0 default (unchanged). Energy already gates (unreadable energy → food dropped) — keep that. The distinction between "absent" and "present-but-bad" is made in the adapter (it knows whether the source key was present); add a small shared helper if it de-duplicates USDA + OFF.

- [ ] **Step 1: Write failing tests** `tests/test_adapter_confidence.py` (fake transport returning crafted payloads, one file covering both adapters): a present-but-non-numeric `proteins_100g`/USDA protein amount → the food is DROPPED (not returned with protein 0.0); a negative macro → dropped; a macro > 100 g/100 g → dropped; kcal absurd (> 1000/100 g) → dropped; an ABSENT macro field → food KEPT with that macro 0.0 (current behavior preserved); a fully-valid food → unaffected.
- [ ] **Step 2: Run** `python -m pytest tests/test_adapter_confidence.py -v` → FAIL.
- [ ] **Step 3: Implement** the present-vs-absent distinction + range sanity in both adapters (shared helper if clean). Keep the energy gate.
- [ ] **Step 4: Run** the new test + regression `python -m pytest tests/test_nutrition_usda_hardening.py tests/test_nutrition_off_hardening.py tests/test_nutrition_food_service.py -q`. If an existing hardening test asserted the OLD "present-but-unparseable → kept with 0.0" behavior, update it to the new drop behavior as part of this task (that was the parked defect, not a contract to preserve).
- [ ] **Step 5: Commit** — `fix(nutrition): drop foods with present-but-untrustworthy macros (confidence hardening)`.

---

### Task 2: Carried Plan-3 cleanups

**Files:**
- Modify: `jarvisd/tools/meal_log.py` (post-log summary out of the catch-all)
- Modify: `security/policy.py` (explicit entries)
- Test: `tests/test_tools_meal_log.py` (add one case), `tests/test_policy_meal_tools.py` (new or extend existing policy test)

**Interfaces:**
- Produces: `log_meal`'s success-summary total is built from values already computed during logging (or computed BEFORE the meal is persisted), so no post-persist code path can return an "Error: couldn't log the meal" after the row exists (removing the theoretical duplicate-log retry). `security/policy.py` classifies `day_status` as read-only and `log_estimated_meal` as write explicitly (today both fall through to the `"write"` default).

- [ ] **Step 1: Write failing/guard tests**: a `log_meal` test asserting that when the meal persists, the handler returns a success string (not an error) even if a post-log summary computation would fail — simplest: assert the summary is derived from the already-computed total, i.e. no second fallible compute after `log_verified_meal`. A policy test asserting `policy.classify("day_status")` (or the module's actual API) is read-only and `log_estimated_meal` is write.
- [ ] **Step 2: Run** → FAIL (or RED on the new policy assertion).
- [ ] **Step 3: Implement**: in `log_meal`, compute the displayed total once (from the resolved ingredients / the repo return) and build the summary outside the broad `except`; add the two `security/policy.py` entries. (Read `security/policy.py` first to match its real mapping API — the earlier review saw a name→permission dict at ~line 58.)
- [ ] **Step 4: Run** the touched tests + regression `python -m pytest tests/ -k "meal_log or policy" -q`.
- [ ] **Step 5: Commit** — `fix(macro-chef): harden log_meal summary path + explicit policy tiers for meal tools`.

---

### Task 3: `validate_dish` + `check_dish` tool

**Files:**
- Create: `jarvisd/tools/suggest.py`
- Test: `tests/test_tools_check_dish.py`

**Interfaces:**
- Consumes: `resolve_ingredient`/`ResolvedIngredient` (import from `jarvisd.tools.meal_log`), `compute_meal`/`compute_ingredient` (engine), `day_status` (repo, `nutrition_core.store.meals`), `remaining`/`assess_day` (budget), `PantryStore.list_items`/`find_by_food`, `Macros`.
- Produces:
  - `DishCheck` (frozen dataclass): `name: str`, `ok: bool` (resolved + validatable), `reason: str | None` (why not, if `ok` is False), `macros: Macros | None` (computed total, rounded), `fit: str` (`'ok'`/`'over'`/`'cornered'`/`'no-target'`), `fit_messages: tuple[str, ...]`, `pantry_ok: bool`, `shortfalls: tuple[str, ...]` (per-ingredient "needs X g, have Y g").
  - `validate_dish(name, ingredients, *, conn, food_service, pantry_store) -> DishCheck` — resolve every ingredient (any failure ⇒ `ok=False`, `reason` names it, no macros); else compute the total; determine pantry availability per ingredient (a `pantry_id` ingredient → that row; a barcode/name ingredient → `find_by_food(source, external_id)`); a tracked row with `grams_available < grams` is a shortfall; compute fit from `day_status(conn, today)["remaining"]` (None ⇒ `fit='no-target'`, else `assess_day(remaining − total)`). **Read-only — no writes, no depletion.**
  - `check_dish` tool: input `name` (string) + `ingredients` (array ≥1, same shape as `log_meal`). Calls `validate_dish`, formats a readable preview: computed macros, fit ("fits — N kcal headroom" / "over by X" / "corners you on protein"), and pantry status (ready to cook / short by X g of Y / uses non-pantry item Z). Never raises. States the dish was NOT logged.
  - `TOOLS` (so far `[check_dish]`) + `handlers(conn, food_service, pantry_store)`.

- [ ] **Step 1: Write failing tests** `tests/test_tools_check_dish.py` (real in-memory DB + PantryStore seeded with items + fake food_service): a valid pantry dish that fits → `fit='ok'`, macros computed, `pantry_ok=True`, preview says fits + ready; a dish over budget → `fit='over'`, over-by message; one unresolvable ingredient → `ok=False`, reason names it, NO macros shown (Review-Focus 2); a dish needing more grams than the pantry holds → `pantry_ok=False`, shortfall message (Review-Focus 3); no active goal → `fit='no-target'`, macros still computed, no crash (Review-Focus 4); assert NO meal row written and pantry grams UNCHANGED after check_dish (Review-Focus 5); bad input (missing name / empty ingredients) → error string, never raises.
- [ ] **Step 2: Run** `python -m pytest tests/test_tools_check_dish.py -v` → FAIL.
- [ ] **Step 3: Implement** `suggest.py` (`DishCheck`, `validate_dish`, `check_dish`, `TOOLS`, `handlers`). Reuse `resolve_ingredient`; do not duplicate resolution logic.
- [ ] **Step 4: Run** → PASS; regression `python -m pytest tests/ -k "suggest or meal_log or pantry" -q`.
- [ ] **Step 5: Commit** — `feat(macro-chef): validate_dish + check_dish (deterministic dish preview)`.

---

### Task 4: `suggest_meals` tool (curated set)

**Files:**
- Modify: `jarvisd/tools/suggest.py`
- Test: `tests/test_tools_suggest_meals.py`

**Interfaces:**
- Consumes: `validate_dish` (Task 3).
- Produces (added to `TOOLS` + `handlers`):
  - `suggest_meals` tool input: `dishes` (array, 1–3) each `{name, slot, ingredients}` where `slot` ∈ {`sweet`,`savory`,`breakfast`} and `ingredients` is the `log_meal` shape. The LLM proposes these from the pantry; the tool `validate_dish`-es each, KEEPS only those that resolve AND fit (`fit` in {`ok`,`no-target`}) AND are pantry-ready (`pantry_ok`), picks the best one per slot (ranking helper `_fit_score` — e.g. highest protein among fitters), and returns the curated set with each dish's real computed macros + a one-line fit note. If NO candidate passes → a clean "Nothing in your pantry fits what's left today — want to create your own, or a different taste?" message (Review-Focus 4). **Read-only — no logging, no depletion** (Review-Focus 5).
  - `_fit_score(check: DishCheck) -> float` — rank fitters (define concretely; protein-forward is fine), used to pick one per slot.

- [ ] **Step 1: Write failing tests** `tests/test_tools_suggest_meals.py`: three candidates (one per slot) all fitting → all three returned, grouped by slot, each with computed macros; two candidates in the same slot → only the better-`_fit_score` one shown; a candidate that's over-budget or pantry-short → excluded from the set; ALL candidates failing → the "nothing fits — create your own" message; a candidate with an unresolvable ingredient → excluded, others still shown (Review-Focus 2); assert NO meal rows and pantry grams unchanged (Review-Focus 5); bad input (empty `dishes`, >3, bad slot, non-list) → error string, never raises.
- [ ] **Step 2: Run** `python -m pytest tests/test_tools_suggest_meals.py -v` → FAIL.
- [ ] **Step 3: Implement** `suggest_meals` + `_fit_score` in `suggest.py`; add to `TOOLS`/`handlers`.
- [ ] **Step 4: Run** → PASS; regression `python -m pytest tests/ -k "suggest" -q`.
- [ ] **Step 5: Commit** — `feat(macro-chef): suggest_meals (curated pantry-fit set, one per slot)`.

---

### Task 5: Wiring

**Files:**
- Modify: `jarvisd/tools/__init__.py` (`build_registry` registers `suggest.TOOLS`/handlers), `jarvisd/core.py`, `jarvisd/bootstrap.py`
- Test: `tests/test_tools_registry_suggest.py`

**Interfaces:**
- Produces: `check_dish`/`suggest_meals` wired whenever the suggestion deps are present. These need the same trio as meal_log (`conn` + `food_service` + `pantry_store`); reuse the EXISTING `meal_log_conn` parameter as the shared conn signal rather than adding a new one — register suggest tools in the same `if meal_log_conn is not None and pantry_store is not None and food_service is not None:` block (or an adjacent block gated identically), calling `suggest.handlers(meal_log_conn, food_service, pantry_store)`. No new bootstrap/Assistant parameter needed if `meal_log_conn` is reused; if a new param reads cleaner, default it `None` and thread it like the others.

- [ ] **Step 1: Write failing test** `tests/test_tools_registry_suggest.py` (mirror `test_tools_registry_meal_log.py`): with `food_service`+`pantry_store`+`meal_log_conn` present → `check_dish`/`suggest_meals` in tools + handlers; default build → absent; registering suggest does not drop food/pantry/meal_log tools.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement**: import `suggest` in the registry module; register its tools in/next to the meal_log gate; confirm `bootstrap` already supplies the trio (it does, from Plan 3) so no bootstrap change may be needed beyond ensuring suggest is wired. Keep all params defaulting None; food/pantry/meal_log blocks untouched.
- [ ] **Step 4: Run** the registry test → PASS; whole suite `python -m pytest tests/ -q` (expect green modulo any environmental port-8765 server tests when the daemon holds the port).
- [ ] **Step 5: Commit** — `feat(macro-chef): wire suggestion tools into JARVIS`.

---

## Done when

JARVIS can propose a curated sweet/savory/breakfast set built from the pantry and sized to the day's remaining macros, where every number and every fit verdict is computed deterministically (`validate_dish`), unresolvable or pantry-short dishes are never offered as ready-to-cook, nothing is auto-logged or auto-depleted, and adapter search no longer passes a silent 0.0 for an untrustworthy macro. Choosing a suggestion logs it through Plan 3's `log_meal`. All new wiring params default `None`; full suite green but for any environmental port-8765 server tests.

**Next:** Plan 5 — Meal-prep + reminders (whole-day plan, lazily committed; meal-prep mode with day-ahead ingredient + prep reminders). After Plan 5 the JARVIS face of the tool-pack is complete; remaining project scope (libSQL embedded replica + rollback() on the DbConnection protocol, FastAPI Engine Service, the PWA) is tracked in project memory.
