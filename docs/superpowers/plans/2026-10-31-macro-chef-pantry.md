# Macro Chef — Pantry Implementation Plan (Plan 2 of 5)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give JARVIS a live pantry — a gram-accurate inventory the user seeds (by barcode, by name, or from a nutrition label) and that later gets auto-depleted when they cook — so meal suggestions can draw from what they actually have, with zero-error per-100g snapshots.

**Architecture:** Extend the `pantry` table (Phase-2) with a per-100g snapshot (migration 8, owned by `nutrition_core`, run by JARVIS). A `nutrition_core/store/pantry.py` repo does the CRUD + a depletion primitive; a thin `jarvisd/pantry_store.py` `PantryStore(conn)` wraps it (mirroring `MealStore`). Food resolution reuses Plan 1's `FoodService` (name → `lookup`, barcode → a new `by_barcode`), or the user supplies per-100g directly (MANUAL, from a label). Pantry tools expose add/list/set-quantity/remove. Depletion-on-cook is the *primitive only* here; wiring it into logging is Plan 3.

**Tech Stack:** Python 3.11, existing `nutrition_core` (engine/models/store, `FoodService`), JARVIS tool-registry pattern, SQLite (`data/jarvis.db`). No new dependency.

**Spec:** `docs/superpowers/specs/2026-10-31-macro-chef-tool-pack-design.md` (esp. §4.4, §8.4). Builds on Plan 1 (`docs/superpowers/plans/2026-10-31-macro-chef-food-lookup.md`, merged).

## Global Constraints

- **Python 3.11.** No new dependency.
- **Zero-error:** a pantry row stores a **per-100g snapshot** (kcal/protein/carbs/fat) captured at add time, so the number is reproducible even if the food DB later changes — mirroring how `meal_ingredients` stores snapshots. All per-100g values come from a food source (USDA/OFF via `FoodService`) OR a user-transcribed nutrition label (source `MANUAL`). The code never invents a macro.
- **Grams are the unit** for all quantities (`grams_available`, deltas).
- **`nutrition_core` opens no DB / no network** — repo functions take `conn: DbConnection`; resolution goes through the injected `FoodService`. Tests build the DB via `from jarvisd.db import connect` + `connect(":memory:")` and use fake adapters/services (no live network).
- **Migrations are additive** (owned by `nutrition_core/store/schema.py`, executed by `jarvisd/db.py` as migration 8; bump `SCHEMA_VERSION` to 8). Never drop/rewrite existing data; existing `pantry` rows (if any) keep working.
- **Tools never raise** — each returns a human-readable string, including on error (follow `jarvisd/tools/meals.py`). New `build_registry`/`Assistant` params default `None`, so existing wiring is unchanged.
- Commits end: `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.

## Review Focus

1. **Depletion past zero / exact empty** — cooking more grams of an item than remain must floor at 0 (or remove the row), never go negative; depleting exactly to 0 is handled. *(Task 3.)*
2. **Barcode / name not found** — `pantry_add` for an unknown barcode or a name with no matches returns a clear "couldn't find that food" string and adds nothing, never a traceback. *(Tasks 2 & 4.)*
3. **MANUAL label entry** — adding by label with user-supplied per-100g stores `source=MANUAL` with those exact numbers and a confidence; it must NOT be silently dropped for lacking an `external_id`. *(Tasks 3 & 4.)*
4. **Duplicate add of the same food** — adding a food already in the pantry (same source+external_id) tops up the grams rather than creating a confusing second row. *(Tasks 3 & 4 — decide + test.)*
5. **Missing/zero/negative grams** — `pantry_add`/`set_quantity` with absent, non-numeric, zero, or negative grams returns an error string and does not write garbage. *(Task 4.)*

---

### Task 1: Migration 8 — per-100g snapshot on `pantry`

**Files:**
- Modify: `nutrition_core/store/schema.py` (add `PANTRY_SNAPSHOT_SQL`)
- Modify: `jarvisd/db.py` (migration 8; `SCHEMA_VERSION = 8`)
- Test: `tests/test_pantry_migration.py`

**Interfaces:**
- Produces: `pantry` gains nullable columns `per100_kcal REAL, per100_protein_g REAL, per100_carbs_g REAL, per100_fat_g REAL, confidence TEXT`. (Existing columns: `id, name, source, external_id, grams_available, updated_at`.)

- [ ] **Step 1: Write failing tests** (class-based, `connect(":memory:")`): a fresh DB reaches `SCHEMA_VERSION == 8`; `PRAGMA table_info(pantry)` includes the five new columns; an existing v7-shape row still readable (additive).
- [ ] **Step 2: Run** `python -m pytest tests/test_pantry_migration.py -v` → FAIL.
- [ ] **Step 3: Implement** `PANTRY_SNAPSHOT_SQL` in `schema.py` = the five `ALTER TABLE pantry ADD COLUMN ...` statements (nullable; `confidence TEXT`). In `db.py`: `MIGRATIONS[8] = PANTRY_SNAPSHOT_SQL` (import it), `SCHEMA_VERSION = 8`. Update `tests/test_db.py`'s version-pin assertion to 8.
- [ ] **Step 4: Run** the migration test + `python -m pytest tests/test_db.py -q` → PASS.
- [ ] **Step 5: Commit** — `feat(nutrition): migration 8 — per-100g snapshot columns on pantry`.

---

### Task 2: `FoodService.by_barcode`

**Files:**
- Modify: `nutrition_core/food_service.py`
- Test: `tests/test_food_service_barcode.py`

**Interfaces:**
- Consumes: the OFF adapter's `get_by_barcode(barcode) -> FoodItem | None` (barcode is OFF-only); `food_cache.cache_food`.
- Produces: `FoodService.by_barcode(self, barcode: str) -> FoodItem | None` — resolves via the OFF adapter, best-effort caches a hit, returns the `FoodItem` or `None` (not found / no OFF adapter configured). Raises `FoodLookupError` only if the adapter raises (network/HTTP).

- [ ] **Step 1: Write failing tests** (fake OFF adapter): barcode hit → returns the `FoodItem` and it's cached (assert via `get_cached`); barcode miss (adapter returns None) → returns None, nothing cached; no OFF configured → returns None; adapter raises → `FoodLookupError`; cache failure doesn't sink a hit (best-effort).
- [ ] **Step 2: Run** `python -m pytest tests/test_food_service_barcode.py -v` → FAIL.
- [ ] **Step 3: Implement** `by_barcode` on `FoodService`: if no `off` adapter, return None; call `self._off.get_by_barcode(barcode)`; on a hit, best-effort `cache_food` (same try/except pattern as `lookup`); return the item or None; let a network exception surface as `FoodLookupError`.
- [ ] **Step 4: Run** → PASS; regression `python -m pytest tests/ -k food_service -q`.
- [ ] **Step 5: Commit** — `feat(macro-chef): FoodService.by_barcode (OFF barcode lookup, cached)`.

---

### Task 3: Pantry repository

**Files:**
- Create: `nutrition_core/store/pantry.py`
- Test: `tests/test_nutrition_store_pantry.py`

**Interfaces:**
- Consumes: `DbConnection`; `Macros`, `FoodSource`, `Confidence` (`models.py`); the extended `pantry` table.
- Produces (free functions taking `conn`):
  - `add_or_topup(conn, *, name, grams, per100g: Macros, source: str, external_id: str | None, confidence: str) -> int` — if a row with the same `(source, external_id)` exists AND `external_id is not None`, ADD `grams` to it (top-up) and refresh the snapshot/`updated_at`; otherwise INSERT a new row. Returns the row id. MANUAL items (`external_id is None`) always insert a new row.
  - `list_items(conn) -> list[dict]` — all rows (id, name, grams_available, source, external_id, per100g fields, confidence), newest-or-name ordered.
  - `set_quantity(conn, pantry_id: int, grams: float) -> None` — set absolute grams (must be ≥ 0; caller validates).
  - `adjust_quantity(conn, pantry_id: int, delta_grams: float) -> float` — add `delta_grams` (negative to deplete); floor the result at 0.0; if the result is 0, DELETE the row; return the remaining grams (0.0 if removed). This is the depletion primitive Plan 3 will call.
  - `find_by_food(conn, *, source: str, external_id: str) -> dict | None` — locate a pantry row by `(source, external_id)` for depletion matching.
  - `remove(conn, pantry_id: int) -> None`.
- Single `conn.commit()` per mutating call.

- [ ] **Step 1: Write failing tests** (class-based, `connect(":memory:")`):
  - add → `list_items` round-trips name/grams/snapshot/source/confidence.
  - `add_or_topup` twice with same `(source, external_id)` → ONE row, grams summed (e.g. 200 + 150 = 350); snapshot refreshed.
  - MANUAL add twice (external_id None) → TWO rows (no accidental merge).
  - `adjust_quantity(-50)` on 200 → 150 returned & stored; `adjust_quantity(-999)` on 150 → 0.0 and row DELETED (Review-Focus 1); `adjust_quantity` to exactly 0 → removed.
  - `set_quantity` sets absolute; `find_by_food` returns the right row or None; `remove` deletes.
- [ ] **Step 2: Run** `python -m pytest tests/test_nutrition_store_pantry.py -v` → FAIL.
- [ ] **Step 3: Implement** `pantry.py`. Store `source` as the string (`FoodSource.X.value` or `"manual"`), `updated_at = datetime('now')`. Read rows by column name. `add_or_topup` top-up uses `UPDATE ... SET grams_available = grams_available + ?, per100_* = ?, updated_at=...` when the `(source, external_id)` row exists.
- [ ] **Step 4: Run** → PASS; regression `python -m pytest tests/test_db.py -q`.
- [ ] **Step 5: Commit** — `feat(nutrition): pantry repository (inventory + depletion primitive)`.

---

### Task 4: Pantry tools + `PantryStore`

**Files:**
- Create: `jarvisd/pantry_store.py` (thin `PantryStore(conn)` wrapper over the repo)
- Create: `jarvisd/tools/pantry.py`
- Test: `tests/test_tools_pantry.py`

**Interfaces:**
- Consumes: the pantry repo (Task 3) via `PantryStore`; `FoodService` (Plan 1 + Task 2) for resolution; `Macros`/`FoodSource`/`Confidence`.
- Produces:
  - `PantryStore(conn)` with methods delegating to the repo: `add_or_topup(...)`, `list_items()`, `set_quantity(id, grams)`, `adjust_quantity(id, delta)`, `find_by_food(source, external_id)`, `remove(id)`.
  - `jarvisd/tools/pantry.py`: `TOOLS = [pantry_add, pantry_list, pantry_set_quantity, pantry_remove]` and `handlers(pantry_store, food_service)` → `{name: fn}`.
- `pantry_add` input (one tool, three resolution modes): `grams` (required, number) plus exactly one of — `barcode` (string → `food_service.by_barcode`), `name` (string → `food_service.lookup`, take the best/first match), or a MANUAL set (`name` + `kcal_per_100g`/`protein_per_100g`/`carbs_per_100g`/`fat_per_100g` → `source=manual`, `confidence=medium`). It reports what it added ("Added 200 g of Chicken breast, raw (USDA)…"). Barcode/name not found → clear "couldn't find…" string, nothing added.

- [ ] **Step 1: Write failing tests** `tests/test_tools_pantry.py` (fake `food_service` + a real in-memory `PantryStore`):
  - `pantry_add` by barcode (fake `by_barcode` hit) → row added with that snapshot; by name (fake `lookup` returns candidates) → adds best match; MANUAL (per-100g given) → `source=manual` row added (Review-Focus 3).
  - barcode/name not found → "couldn't find" string, pantry unchanged (Review-Focus 2).
  - missing/zero/negative/non-numeric `grams` → error string, nothing written (Review-Focus 5).
  - `pantry_list` empty → "pantry is empty"; with items → lines of name/grams/source.
  - `pantry_set_quantity` valid; negative → error. `pantry_remove` by id.
  - duplicate add of same food tops up (Review-Focus 4) — via the repo's `add_or_topup`.
- [ ] **Step 2: Run** `python -m pytest tests/test_tools_pantry.py -v` → FAIL.
- [ ] **Step 3: Implement** `PantryStore` + `pantry.py`. Validate inputs (grams a positive number; exactly one resolution mode). Resolve via `food_service`; on `FoodLookupError` return a graceful "couldn't reach the food database" string. If `food_service is None` (resolution unavailable), barcode/name modes return a clear "food lookup isn't available — add it manually with per-100g values" string while MANUAL mode still works. Never raise. On a name lookup with multiple candidates, take the first and state which it used.
- [ ] **Step 4: Run** → PASS; regression `python -m pytest tests/ -k "pantry or food" -q`.
- [ ] **Step 5: Commit** — `feat(macro-chef): pantry tools (add/list/set/remove) + PantryStore`.

---

### Task 5: Wiring

**Files:**
- Modify: `jarvisd/tools/__init__.py` (`build_registry` gains `pantry_store=None`; registers pantry tools when `pantry_store` AND `food_service` present)
- Modify: `jarvisd/core.py` (`Assistant.__init__` forwards `pantry_store=None`)
- Modify: `jarvisd/bootstrap.py` (build `PantryStore(conn)` from the existing `conn`; pass it in)
- Test: `tests/test_tools_registry_pantry.py`

**Interfaces:**
- Produces: pantry tools wired whenever `pantry_store` (and `food_service`) are present; all new params default `None` (non-regression).

- [ ] **Step 1: Write failing test**: `build_registry(..., food_service=<fake>, pantry_store=<fake>)` → `pantry_add`/`pantry_list` in tools + handlers; default (neither) → absent. (Pantry needs `food_service` for resolution — if `pantry_store` is present but `food_service` is None, register pantry tools anyway but MANUAL-only still works; decide and test: register when `pantry_store is not None`.)
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement**: `build_registry` adds `pantry_store=None`; after the food block, `if pantry_store is not None: tools += list(pantry.TOOLS); handler_map.update(pantry.handlers(pantry_store, food_service))`. `Assistant.__init__` takes + forwards `pantry_store=None` (append last, like `food_service`). `bootstrap.py`: `pantry_store = PantryStore(conn)` (same `conn`), passed into `Assistant(...)`. Import `pantry` in the registry module.
- [ ] **Step 4: Run** the registry test → PASS; whole suite `python -m pytest tests/ -q` (expect only the 3 pre-existing port-8765 failures).
- [ ] **Step 5: Commit** — `feat(macro-chef): wire pantry into JARVIS (registry + bootstrap)`.

---

## Done when

JARVIS has a working pantry: the user adds items by barcode, by name, or from a label (grams + per-100g snapshot), lists them, adjusts/removes them, and the repo exposes an `adjust_quantity` depletion primitive (floors at 0, removes at empty) ready for Plan 3 to call on cook. Migration 8 is additive; all new wiring params default `None`; full suite green but for the pre-existing port-8765 server tests.

**Next:** Plan 3 — Logging upgrade + daily status (the three DB-sourced log paths: verified / create-your-own / estimated; pantry depletion on a cooked meal via `adjust_quantity`; verified-vs-estimated split; `day_status`). Then Plan 4 — Suggestions; Plan 5 — Meal-prep + reminders. (Carried from Plan 1: before Plan 4 trusts the numbers, resolve the parked "present-but-unparseable macro → 0.0" + range-sanity item, and the "partial outage reads as no-matches" nuance.)
