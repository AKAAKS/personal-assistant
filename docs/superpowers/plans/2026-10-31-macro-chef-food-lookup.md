# Macro Chef — Food Lookup Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give JARVIS a live, cached, zero-error food-lookup capability — resolve any food's accurate per-100g macros from USDA FoodData Central + Open Food Facts — as the foundation the pantry, logging, and suggestion phases build on.

**Architecture:** A concrete HTTP `Transport` (using the existing `requests` dep) backs the already-merged Phase-1 adapters, which this plan first *hardens* against malformed live responses (the gated Phase-1 ruling). A `food_cache` repo persists resolved foods (Phase-2 table). A `FoodService` composes adapters + cache, and a `lookup_food` JARVIS tool exposes it. USDA is used only when an API key is present; otherwise it degrades to Open Food Facts alone.

**Tech Stack:** Python 3.11, `requests` (already a dependency), existing `nutrition_core` adapters/models, JARVIS tool-registry pattern (`jarvisd/tools/`), SQLite (`data/jarvis.db`).

**Spec:** `docs/superpowers/specs/2026-10-31-macro-chef-tool-pack-design.md` (esp. §3 Constraints, §4.2, §6, §7). Parent architecture: `docs/superpowers/specs/2026-08-26-macro-chef-design.md`.

## Global Constraints

- **Python 3.11.** No new third-party dependency — use `requests` (already in `requirements.txt`).
- **Zero-error rule:** every macro number comes from a food source's own per-100g data via `nutrition_core`; a malformed/garbage response **drops the food**, never invents a number and never raises out of an adapter.
- **No live network in tests:** inject a fake transport/session; adapters and the service are tested against fixtures only.
- **USDA needs a free API key** (`USDA_API_KEY` in `.env`); Open Food Facts needs none. Missing key ⇒ OFF-only, not a hard failure.
- **Follow JARVIS conventions:** each tool module exports a `TOOLS` list + `handlers(deps)`; tools never raise — they return a human-readable string (including on error). Config via `python-dotenv` + `os.getenv`.
- **Caching:** resolved foods persist to the existing `food_cache` table (PK `(source, external_id)`); confidence is derived from source on read (USDA→HIGH, OPEN_FOOD_FACTS→MEDIUM).
- Commits end: `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.

## Review Focus

1. **Malformed live payload** (gated): `get_json` returns `None`, a list, or a dict whose `foods`/`products` is `None`/a dict/a list of non-dicts, or a nutrient `value`/`nutrientId` that is a non-numeric string → the adapter drops the food and returns a (possibly empty) list, never raises. *(Tasks 2 & 3.)*
2. **Missing `USDA_API_KEY`** → `FoodService` is built with OFF only; `lookup_food` still works, never errors about a missing key. *(Task 5/6.)*
3. **Network failure / timeout** during a lookup → `lookup_food` returns a graceful "couldn't reach the food database" message, not a traceback; one source failing still lets the other's results through. *(Tasks 1 & 5.)*
4. **Zero results** for a query → `lookup_food` returns a clear "no matches for X", not an error or empty string. *(Task 5.)*
5. **Duplicate cache write** (same `source`+`external_id`, e.g. re-looking-up the same food) → upsert replaces, no PRIMARY KEY violation; an item with `external_id = None` is simply not cached. *(Task 4.)*

---

### Task 1: HTTP transport

**Files:**
- Create: `nutrition_core/adapters/http.py`
- Test: `tests/test_nutrition_http_transport.py`

**Interfaces:**
- Consumes: the `Transport` protocol in `nutrition_core/adapters/base.py` (`get_json(url, params=None) -> dict`).
- Produces: `RequestsTransport(*, timeout: float = 10.0, session=None)` with `get_json(self, url: str, params: dict | None = None) -> dict`. A later task/bootstrap constructs one and hands it to the adapters.

- [ ] **Step 1: Write the failing tests** in `tests/test_nutrition_http_transport.py` (class-based). Use a fake session (a small object with a `get(url, params=, timeout=)` returning a fake response exposing `raise_for_status()` and `json()`):
  - `test_get_json_passes_params_and_returns_parsed_json`: returns the fake response's `.json()` dict; asserts the fake session received the url, the params dict, and the configured timeout.
  - `test_get_json_raises_for_http_error`: fake response's `raise_for_status()` raises `requests.HTTPError` → `get_json` propagates it (the caller/tool handles it; the transport does not swallow).
- [ ] **Step 2: Run** `python -m pytest tests/test_nutrition_http_transport.py -v` → FAIL (module missing).
- [ ] **Step 3: Implement** `RequestsTransport` in `nutrition_core/adapters/http.py`. `get_json` calls `(self._session or requests).get(url, params=params, timeout=self._timeout)`, then `resp.raise_for_status()`, then `return resp.json()`. `session` defaults to `None` (module-level `requests`), injectable for tests.
- [ ] **Step 4: Run** the tests → PASS.
- [ ] **Step 5: Commit** — `feat(nutrition): requests-backed HTTP transport for food adapters`.

---

### Task 2: Harden the USDA adapter against malformed payloads

**Files:**
- Modify: `nutrition_core/adapters/usda.py`
- Test: `tests/test_nutrition_usda_hardening.py`

**Interfaces:**
- Produces: unchanged public surface — `USDAAdapter(transport, api_key).search(query, *, limit=5) -> list[FoodItem]`. Behavior change only: never raises on a malformed payload.

- [ ] **Step 1: Write the failing tests** (class-based), each using a fake transport returning a fixed payload; assert `search(...)` returns `[]` (or skips only the bad entry) and raises nothing:
  - payload is `None`; payload is a list; `payload["foods"]` is `None`; `foods` is a dict; a `food` entry is a string; `foodNutrients` is `None`/a dict; a nutrient entry is a string; a nutrient `value` is `"abc"`; a `nutrientId` is `None`.
  - `test_valid_payload_still_parsed`: a well-formed one-food payload still yields one `FoodItem` with the right per-100g macros (guards against over-filtering).
- [ ] **Step 2: Run** `python -m pytest tests/test_nutrition_usda_hardening.py -v` → FAIL (raises today).
- [ ] **Step 3: Implement** guards in `usda.py`: coerce a non-dict payload to `{}`; iterate `foods` only when it is a list; skip any `food` that is not a dict; in `_nutrient_values` iterate only a list and skip non-dict entries; wrap the `int(number)`/`float(value)` conversion in `try/except (TypeError, ValueError)` and skip on failure. Drop a food whose `macros_from_fields(...)` is `None` (already the case). No new public behavior beyond robustness.
- [ ] **Step 4: Run** the tests → PASS. Then regression: `python -m pytest tests/ -k usda -q`.
- [ ] **Step 5: Commit** — `fix(nutrition): harden USDA adapter against malformed payloads`.

---

### Task 3: Harden the Open Food Facts adapter against malformed payloads

**Files:**
- Modify: `nutrition_core/adapters/openfoodfacts.py`
- Test: `tests/test_nutrition_off_hardening.py`

**Interfaces:**
- Produces: unchanged surface — `OpenFoodFactsAdapter(transport).search(query, *, limit=5) -> list[FoodItem]` and `get_by_barcode(barcode) -> FoodItem | None`. Behavior change only: never raises on a malformed payload.

- [ ] **Step 1: Write the failing tests** (class-based), fake transport per case; assert `[]`/`None` and no raise:
  - `search`: payload is `None`; a list; `payload["products"]` is `None`/a dict; a `product` entry is a string.
  - `get_by_barcode`: payload is `None`; `status` missing; `status != 1`; `product` is a string.
  - `test_valid_product_still_parsed` (search and barcode) still yields the expected `FoodItem` (per-100g correct; kJ→kcal path intact).
- [ ] **Step 2: Run** `python -m pytest tests/test_nutrition_off_hardening.py -v` → FAIL.
- [ ] **Step 3: Implement** guards in `openfoodfacts.py`: coerce a non-dict `payload` to `{}` in both methods; iterate `products` only when it is a list; skip a non-dict `product` in `_to_food_item` (return `None`). `_number` already rejects non-numeric/non-finite — keep it.
- [ ] **Step 4: Run** the tests → PASS. Regression: `python -m pytest tests/ -k "off or openfood" -q`.
- [ ] **Step 5: Commit** — `fix(nutrition): harden Open Food Facts adapter against malformed payloads`.

---

### Task 4: Food cache repository

**Files:**
- Create: `nutrition_core/store/food_cache.py`
- Test: `tests/test_nutrition_store_food_cache.py`

**Interfaces:**
- Consumes: `DbConnection` (`nutrition_core/store/connection.py`); `FoodItem`, `Macros`, `FoodSource`, `Confidence` (`nutrition_core/models.py`); the `food_cache` table (schema.py — PK `(source, external_id)`, columns `name, per100_kcal, per100_protein_g, per100_carbs_g, per100_fat_g, fetched_at`).
- Produces:
  - `cache_food(conn, item: FoodItem) -> bool` — upsert; returns `False` (not cached) when `item.external_id is None`, else `True`.
  - `get_cached(conn, source: str, external_id: str) -> FoodItem | None` — reconstruct a `FoodItem`; confidence derived from source (`USDA`→HIGH, `OPEN_FOOD_FACTS`→MEDIUM, else LOW).

- [ ] **Step 1: Write the failing tests** (class-based; build the DB with `from jarvisd.db import connect` + `connect(":memory:")`):
  - `test_cache_and_get_roundtrip`: `cache_food` a USDA `FoodItem` (external_id="123", per100g 165/31/0/3.6), then `get_cached("usda","123")` returns an equal `FoodItem` with `confidence == Confidence.HIGH` and `source == FoodSource.USDA`.
  - `test_get_missing_returns_none`.
  - `test_upsert_replaces_same_key`: cache twice with different macros for the same `(source, external_id)` → one row, latest macros; no PK error.
  - `test_item_without_external_id_not_cached`: `cache_food` returns `False`, table stays empty.
  - `test_off_confidence_is_medium`.
- [ ] **Step 2: Run** `python -m pytest tests/test_nutrition_store_food_cache.py -v` → FAIL.
- [ ] **Step 3: Implement** `food_cache.py`. Store `item.source.value` as `source`. Upsert via `INSERT ... ON CONFLICT(source, external_id) DO UPDATE SET ...`; set `fetched_at = datetime('now')`; single `conn.commit()`. `get_cached` reads by column name, rebuilds `Macros(per100_kcal, per100_protein_g, per100_carbs_g, per100_fat_g)`, maps source string back to `FoodSource` and to a `Confidence` via a small dict.
- [ ] **Step 4: Run** the tests → PASS.
- [ ] **Step 5: Commit** — `feat(nutrition): food_cache repository (persist resolved foods)`.

---

### Task 5: FoodService + `lookup_food` tool

**Files:**
- Create: `nutrition_core/food_service.py`
- Create: `jarvisd/tools/food.py`
- Test: `tests/test_nutrition_food_service.py`, `tests/test_tools_food.py`

**Interfaces:**
- Consumes: `USDAAdapter`, `OpenFoodFactsAdapter` (Tasks 2/3), `food_cache` (Task 4), a `DbConnection`.
- Produces:
  - `FoodService(conn, *, usda=None, off=None)` with `lookup(self, query: str, *, limit: int = 5) -> list[FoodItem]` — queries each configured adapter (USDA first when present, then OFF), caches every returned item (`cache_food`), and returns the combined list truncated to `limit`. An adapter raising (network/HTTP) is caught: its results are skipped, the other adapter's still returned; if *all* configured adapters fail, re-raise a `FoodLookupError` (define it here) so the tool can report it.
  - `jarvisd/tools/food.py`: `TOOLS = [lookup_food]` and `handlers(food_service)` returning `{"lookup_food": ...}`.
- Downstream (later plans) rely on `FoodService.lookup` and on cached foods being retrievable by `(source, external_id)`.

- [ ] **Step 1: Write the failing tests:**
  - `test_nutrition_food_service.py` (fake adapters — small objects whose `search` returns canned `FoodItem`s or raises):
    - `test_lookup_combines_and_caches`: USDA returns 1, OFF returns 1 → `lookup` returns both, and both are now in `food_cache` (assert via `get_cached`).
    - `test_lookup_off_only_when_no_usda`: `FoodService(conn, off=...)` (usda=None) → only OFF queried, works.
    - `test_one_adapter_failure_still_returns_other`: USDA `.search` raises → OFF results still returned.
    - `test_all_adapters_fail_raises_foodlookuperror`.
    - `test_limit_truncates_combined`.
  - `test_tools_food.py` (fake service):
    - `test_lookup_food_lists_candidates`: returns a string containing each candidate's name + per-100g kcal/P/C/F and its source/id.
    - `test_lookup_food_no_results`: empty list → "no matches" style message, not an error.
    - `test_lookup_food_service_error`: service raises `FoodLookupError` → graceful "couldn't reach the food database" string, no traceback.
    - `test_lookup_food_requires_query`: missing/blank `query` → error string.
- [ ] **Step 2: Run** both test files → FAIL.
- [ ] **Step 3: Implement** `FoodService` (catch per-adapter `Exception`, collect results, cache each via `cache_food`, raise `FoodLookupError` only if every configured adapter failed) and `jarvisd/tools/food.py`. The `lookup_food` tool def: one required `query` string (+ optional `limit` int); handler validates `query`, calls `food_service.lookup`, formats candidates (name — `kcal`/`P`/`C`/`F` per 100g — `source:external_id`), returns the never-raise string.
- [ ] **Step 4: Run** both → PASS.
- [ ] **Step 5: Commit** — `feat(macro-chef): FoodService + lookup_food tool (USDA+OFF, cached)`.

---

### Task 6: Bootstrap wiring + config

**Files:**
- Modify: `jarvisd/bootstrap.py` (build the transport/adapters/service from env + the existing `conn`, pass to the registry)
- Modify: `jarvisd/tools/__init__.py` (`build_registry` gains a `food_service=None` param; wires `food.TOOLS`/`handlers` when present)
- Modify: `.env.example` (document `USDA_API_KEY`)
- Test: `tests/test_tools_registry_food.py`

**Interfaces:**
- Consumes: `FoodService` (Task 5), `RequestsTransport` (Task 1), `USDAAdapter`/`OpenFoodFactsAdapter`.
- Produces: a wired `lookup_food` tool whenever `food_service` is passed; `build_registry(..., food_service=None)` adds `food.TOOLS` + handlers when it is not `None`.

- [ ] **Step 1: Write the failing test** `tests/test_tools_registry_food.py`:
  - `test_registry_includes_lookup_food_when_service_present`: `build_registry(ha_client=..., system_client=..., food_service=<fake>)` → `"lookup_food"` in the handler map and a tool named `lookup_food` in the tools list.
  - `test_registry_omits_food_when_service_none`: default (no `food_service`) → no `lookup_food`.
  - (Reuse the registry's existing test doubles/pattern for `ha_client`/`system_client`.)
- [ ] **Step 2: Run** `python -m pytest tests/test_tools_registry_food.py -v` → FAIL.
- [ ] **Step 3: Implement:**
  - `build_registry`: add `food_service=None`; after the `meal_store` block, `if food_service is not None: tools += list(food.TOOLS); handler_map.update(food.handlers(food_service))`. Import `food` alongside the other tool modules.
  - `bootstrap.py`: read `usda_key = os.getenv("USDA_API_KEY")`; build `transport = RequestsTransport()`; `usda = USDAAdapter(transport, usda_key) if usda_key else None`; `off = OpenFoodFactsAdapter(transport)`; `food_service = FoodService(conn, usda=usda, off=off)`; pass `food_service=food_service` into `build_registry`. (Reuse the existing `conn` from `db.connect(...)`.)
  - `.env.example`: add, near the other keys —
    `# USDA FoodData Central API key (free: https://fdc.nal.usda.gov/api-key-signup.html).`
    `# Unset = Open Food Facts only (branded foods); lookup still works.`
    `# USDA_API_KEY=your-usda-fdc-api-key-here`
- [ ] **Step 4: Run** the registry test → PASS. Then the whole suite: `python -m pytest tests/ -q` (expect only the pre-existing 3 port-8765 server failures).
- [ ] **Step 5: Commit** — `feat(macro-chef): wire food lookup into JARVIS (env + registry)`.

---

## Done when

`lookup_food` is a live JARVIS tool: given a food name it returns accurate per-100g macros from USDA (when keyed) and Open Food Facts, caches them, and degrades gracefully when the key is absent, the network fails, or a payload is garbage — never inventing a number and never crashing. Full suite green but for the pre-existing port-8765 server tests. This is the resolution of Phase-1's gated adapter-hardening ruling.

**Next plans (roadmap, each shippable on its own):** Plan 2 — Pantry (CRUD by barcode/name/label + per-100g snapshots + depletion hooks). Plan 3 — Logging upgrade + daily status (verified / create-your-own / estimated paths; verified-vs-estimated split; pantry depletion on cook). Plan 4 — Suggestions (`suggest_meal` steering context + deterministic `check_dish` validator; curated sweet/savory/generic; swap; budget integrity). Plan 5 — Meal-prep + reminders (`planned` status; aggregate shopping delta; day-ahead ingredient/prep reminders). Profile (informational) folds into Plan 2 or 3.
