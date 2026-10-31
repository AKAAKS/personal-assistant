# Nutrition Core (Phase 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `nutrition_core`, a pure Python library that turns food lookups and gram amounts into verified, deterministic calorie/macro numbers.

**Architecture:** A standalone package with no LLM, no chat, no persistence, and no network at test time. Immutable data models carry macros; pure engine functions do all arithmetic; a budget module assesses day fit; food adapters normalize USDA and Open Food Facts responses (fetched through an injected transport) into one `FoodItem` shape. This is the shared accuracy engine both faces (JARVIS, the PWA) will reuse.

**Tech Stack:** Python 3.11, `dataclasses`, `enum`, `typing.Protocol`, pytest (class-based tests + fixtures, matching the repo convention in `tests/`).

**Spec:** `docs/superpowers/specs/2026-08-26-macro-chef-design.md` (Phase 1 / "nutrition_core — the shared engine"). Persistence, the abstract connection interface, the `MealStore`→libSQL migration, live HTTP, the food cache, the JARVIS tool-pack, the FastAPI service, and the PWA are all OUT OF SCOPE for this plan and belong to later plans.

## Global Constraints

- **Language/version:** Python 3.11; use `X | None` syntax and `from __future__ import annotations` is unnecessary.
- **No LLM, no chat, no network, no persistence inside `nutrition_core`** (spec Core Principle 2). Adapters receive an injected `Transport`; tests never hit the network.
- **The library never invents a macro number** (spec Core Principle 1): every `FoodItem` carries its `source` and `confidence`; every computed macro derives from `grams × per-100g`.
- **Package location:** top-level `nutrition_core/` in the `personal-assistant` repo, importable as `import nutrition_core`.
- **Tests:** pytest, class-based grouping (`class TestX:`), fixtures for shared setup, one `tests/test_nutrition_*.py` file per module (flat, matching existing repo layout). Recorded API responses live in `tests/fixtures/nutrition/`.
- **Macros as the one numeric type:** per-100g values, meal totals, daily goals, eaten totals, remaining budgets, and deltas are ALL represented by `Macros` (deltas/remaining may be negative). Do not introduce parallel types for these.

## Review Focus

- **Zero / negative grams** (engine): `compute_ingredient(grams=0)` yields all-zero macros; negative grams raise `ValueError` rather than producing negative food. → Task 2.
- **Missing nutrient fields in API responses** (adapters): a USDA food with no energy nutrient, or an OFF product missing `proteins_100g`, must be skipped or flagged (confidence/omission), never silently treated as 0 and returned as a trustworthy number. → Tasks 5, 6.
- **Open Food Facts energy in kJ only** (OFF adapter): when a product has `energy-kj_100g`/`energy_100g` but no `energy-kcal_100g`, convert kJ→kcal (`kcal = kJ / 4.184`); do not report kJ as kcal. → Task 6.
- **Rounding delta is retrievable, not hidden** (engine): after `round_portion` changes grams, recomputing yields macros that differ from the unrounded request, and that difference is obtainable (the face must be able to show it). → Task 2.
- **Cornered day at the tolerance boundary** (budget): when remaining calories are just below the minimum calories the remaining macros physically require, `assess_day` returns `"cornered"`; just above, `"ok"`. → Task 3.

---

### Task 1: Data models

**Files:**
- Create: `nutrition_core/__init__.py` (empty or version string)
- Create: `nutrition_core/models.py`
- Test: `tests/test_nutrition_models.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `class FoodSource(str, Enum)` with members `USDA="usda"`, `OPEN_FOOD_FACTS="open_food_facts"`, `MANUAL="manual"`.
  - `class Confidence(str, Enum)` with members `LOW="low"`, `MEDIUM="medium"`, `HIGH="high"`.
  - `@dataclass(frozen=True) class Macros` with fields `kcal: float`, `protein_g: float`, `carbs_g: float`, `fat_g: float`; methods `__add__(self, other: Macros) -> Macros` (elementwise), `__sub__(self, other: Macros) -> Macros` (elementwise, may go negative), `scaled(self, factor: float) -> Macros` (each field × factor), `rounded(self, ndigits: int = 1) -> Macros`.
  - `@dataclass(frozen=True) class FoodItem` with `name: str`, `per100g: Macros`, `source: FoodSource`, `external_id: str | None`, `confidence: Confidence`.
  - `@dataclass(frozen=True) class ComputedIngredient` with `name: str`, `grams: float`, `macros: Macros`, `source: FoodSource`.
  - `@dataclass(frozen=True) class MealComputation` with `ingredients: tuple[ComputedIngredient, ...]`, `total: Macros`.

- [ ] **Step 1: Write the failing test**

```python
class TestMacrosArithmetic:
    def test_add_is_elementwise(self):
        a = Macros(100, 10, 20, 5)
        b = Macros(50, 5, 0, 2)
        assert a + b == Macros(150, 15, 20, 7)

    def test_sub_allows_negative(self):
        assert (Macros(100, 10, 20, 5) - Macros(120, 4, 8, 9)) == Macros(-20, 6, 12, -4)

    def test_scaled_multiplies_each_field(self):
        assert Macros(100, 10, 20, 5).scaled(0.5) == Macros(50, 5, 10, 2.5)

    def test_rounded_rounds_each_field(self):
        assert Macros(100.04, 10.06, 20.0, 5.55).rounded(1) == Macros(100.0, 10.1, 20.0, 5.6)
```

- [ ] **Step 2: Run test to verify it fails** — `pytest tests/test_nutrition_models.py -v`; Expected: FAIL (`Macros` not defined).
- [ ] **Step 3: Implement `models.py`** with the enums and frozen dataclasses from the Interfaces block. `Macros` arithmetic operates field-by-field.
- [ ] **Step 4: Run test to verify it passes** — Expected: PASS.
- [ ] **Step 5: Commit** — `git add nutrition_core/ tests/test_nutrition_models.py && git commit -m "feat(nutrition): data models and Macros arithmetic"`

---

### Task 2: Nutrition Engine

**Files:**
- Create: `nutrition_core/engine.py`
- Test: `tests/test_nutrition_engine.py`

**Interfaces:**
- Consumes: `Macros`, `ComputedIngredient`, `MealComputation`, `FoodSource` from Task 1.
- Produces:
  - `def compute_ingredient(name: str, grams: float, per100g: Macros, source: FoodSource) -> ComputedIngredient` — `macros = per100g.scaled(grams / 100)`. Raises `ValueError` if `grams < 0`.
  - `def compute_meal(ingredients: Sequence[ComputedIngredient]) -> MealComputation` — `total` is the elementwise sum of ingredient macros (all-zero `Macros` for an empty sequence).
  - `def round_portion(grams: float, *, step_g: float = 5.0, unit_g: float | None = None) -> float` — if `unit_g` is given, round to the nearest whole multiple of `unit_g`; else round to the nearest `step_g`. For a positive input the result is never 0 (minimum one step/unit). Raises `ValueError` if `grams < 0`.

- [ ] **Step 1: Write the failing test**

```python
class TestComputeIngredient:
    def test_scales_per_100g_by_grams(self):
        ci = compute_ingredient("chicken breast", 140, Macros(165, 31, 0, 3.6), FoodSource.USDA)
        assert ci.macros == Macros(231.0, 43.4, 0.0, 5.04)

    def test_zero_grams_is_all_zero(self):
        ci = compute_ingredient("rice", 0, Macros(130, 2.7, 28, 0.3), FoodSource.USDA)
        assert ci.macros == Macros(0, 0, 0, 0)

    def test_negative_grams_raises(self):
        with pytest.raises(ValueError):
            compute_ingredient("rice", -5, Macros(130, 2.7, 28, 0.3), FoodSource.USDA)

class TestComputeMeal:
    def test_totals_are_elementwise_sum(self):
        ings = [
            compute_ingredient("chicken", 100, Macros(165, 31, 0, 3.6), FoodSource.USDA),
            compute_ingredient("rice", 100, Macros(130, 2.7, 28, 0.3), FoodSource.USDA),
        ]
        assert compute_meal(ings).total == Macros(295, 33.7, 28, 3.9)

    def test_empty_meal_is_zero(self):
        assert compute_meal([]).total == Macros(0, 0, 0, 0)

class TestRoundPortion:
    def test_rounds_to_nearest_step(self):
        assert round_portion(137) == 135.0
        assert round_portion(138) == 140.0

    def test_unit_rounding_whole_items(self):
        assert round_portion(130, unit_g=50) == 150.0  # ~2.6 eggs -> 3 units

    def test_positive_input_never_rounds_to_zero(self):
        assert round_portion(2) == 5.0

    def test_rounding_delta_is_observable(self):
        per100 = Macros(155, 13, 1.1, 11)
        requested = compute_ingredient("egg", 132, per100, FoodSource.USDA)
        rounded = compute_ingredient("egg", round_portion(132, unit_g=50), per100, FoodSource.USDA)
        assert rounded.macros != requested.macros  # delta exists and is recomputable
```

- [ ] **Step 2: Run test to verify it fails** — `pytest tests/test_nutrition_engine.py -v`; Expected: FAIL (import error).
- [ ] **Step 3: Implement `engine.py`** per the Interfaces block. `round_portion` uses `round(grams / step) * step` (or `unit_g`), clamped to a minimum of one step/unit for positive input.
- [ ] **Step 4: Run test to verify it passes** — Expected: PASS.
- [ ] **Step 5: Commit** — `git commit -m "feat(nutrition): deterministic engine (compute + practical rounding)"`

---

### Task 3: Budget guard

**Files:**
- Create: `nutrition_core/budget.py`
- Test: `tests/test_nutrition_budget.py`

**Interfaces:**
- Consumes: `Macros` from Task 1.
- Produces:
  - `def remaining(goal: Macros, eaten: Macros) -> Macros` — `goal - eaten`.
  - `def after_meal(remaining_now: Macros, meal_total: Macros) -> Macros` — `remaining_now - meal_total`.
  - `def min_kcal_for(macros: Macros) -> float` — `4*protein_g + 4*carbs_g + 9*fat_g` (Atwater floor; negative macro components contribute 0).
  - `@dataclass(frozen=True) class FitAssessment` with `status: Literal["ok", "over", "cornered"]` and `messages: tuple[str, ...]`.
  - `def assess_day(after: Macros, *, kcal_tolerance: float = 50.0) -> FitAssessment` — see Step 3 for the decision order.

- [ ] **Step 1: Write the failing test**

```python
class TestRemaining:
    def test_remaining_subtracts(self):
        assert remaining(Macros(2000, 150, 200, 60), Macros(500, 40, 50, 15)) == Macros(1500, 110, 150, 45)

class TestAssessDay:
    def test_ok_when_everything_fits(self):
        assert assess_day(Macros(800, 50, 80, 25)).status == "ok"

    def test_over_when_a_macro_is_exceeded(self):
        a = assess_day(Macros(-120, 10, 10, 5))  # ate more kcal than budget
        assert a.status == "over"

    def test_cornered_when_calories_cannot_hold_remaining_macros(self):
        # 4*120 + 4*0 + 9*0 = 480 kcal minimum, only 300 left -> impossible
        assert assess_day(Macros(300, 120, 0, 0)).status == "cornered"

    def test_boundary_ok_just_above_min_kcal(self):
        # min = 480; 480 + tolerance margin -> ok
        assert assess_day(Macros(520, 120, 0, 0)).status == "ok"
```

- [ ] **Step 2: Run test to verify it fails** — Expected: FAIL (import error).
- [ ] **Step 3: Implement `budget.py`.** Decision order in `assess_day`: (1) if any of `after.kcal/protein_g/carbs_g/fat_g < -kcal_tolerance` (kcal) or `< 0` (macros) → `"over"`; (2) elif `after.kcal + kcal_tolerance < min_kcal_for(after)` → `"cornered"`; (3) else `"ok"`. Populate `messages` with a short human-readable reason for non-`ok` statuses.
- [ ] **Step 4: Run test to verify it passes** — Expected: PASS.
- [ ] **Step 5: Commit** — `git commit -m "feat(nutrition): budget guard with corner detection"`

---

### Task 4: Adapter base (transport + normalization contract)

**Files:**
- Create: `nutrition_core/adapters/__init__.py`
- Create: `nutrition_core/adapters/base.py`
- Test: `tests/test_nutrition_adapter_base.py`

**Interfaces:**
- Consumes: `Macros`, `FoodItem`, `FoodSource`, `Confidence` from Task 1.
- Produces:
  - `class Transport(Protocol): def get_json(self, url: str, params: dict | None = None) -> dict: ...`
  - `class FoodAdapter(Protocol): def search(self, query: str, *, limit: int = 5) -> list[FoodItem]: ...`
  - `def macros_from_fields(kcal, protein_g, carbs_g, fat_g) -> Macros | None` — each arg is `float | None`; returns `None` if the energy value (`kcal`) is missing, else a `Macros` with missing macro components defaulted to `0.0`. (Energy is the one field that must be present for a number to be trustworthy; a food with no energy is dropped by callers.)

- [ ] **Step 1: Write the failing test**

```python
class TestMacrosFromFields:
    def test_builds_macros_when_energy_present(self):
        assert macros_from_fields(165, 31, None, 3.6) == Macros(165, 31, 0.0, 3.6)

    def test_returns_none_when_energy_missing(self):
        assert macros_from_fields(None, 31, 0, 3.6) is None
```

- [ ] **Step 2: Run test to verify it fails** — Expected: FAIL (import error).
- [ ] **Step 3: Implement `base.py`** with the `Transport` and `FoodAdapter` Protocols and `macros_from_fields`.
- [ ] **Step 4: Run test to verify it passes** — Expected: PASS.
- [ ] **Step 5: Commit** — `git commit -m "feat(nutrition): adapter transport protocol and field normalization"`

---

### Task 5: USDA FoodData Central adapter

**Files:**
- Create: `nutrition_core/adapters/usda.py`
- Create: `tests/fixtures/nutrition/usda_search_chicken.json` (a trimmed real `/foods/search` response with exactly 3 foods: two carrying nutrient numbers 1008/1003/1005/1004, and one lacking 1008 whose `fdcId` is `"NO_ENERGY_FDCID"`)
- Test: `tests/test_nutrition_adapter_usda.py`

**Interfaces:**
- Consumes: `Transport`, `macros_from_fields`, `FoodAdapter` (Task 4); `FoodItem`, `FoodSource`, `Confidence` (Task 1).
- Produces:
  - `class USDAAdapter` with `__init__(self, transport: Transport, api_key: str)` and `search(self, query: str, *, limit: int = 5) -> list[FoodItem]`. Maps USDA nutrient numbers → fields: `1008`→kcal, `1003`→protein_g, `1005`→carbs_g, `1004`→fat_g (values are per 100g). Sets `source=FoodSource.USDA`, `external_id=str(fdcId)`, `confidence=Confidence.HIGH`. Foods whose energy (1008) is absent (so `macros_from_fields` returns `None`) are omitted from the results.

- [ ] **Step 1: Write the failing test** (uses a fake transport returning the fixture)

```python
class FakeTransport:
    def __init__(self, payload): self._payload = payload
    def get_json(self, url, params=None): return self._payload

class TestUSDAAdapter:
    def test_parses_foods_into_fooditems(self, usda_chicken_payload):
        items = USDAAdapter(FakeTransport(usda_chicken_payload), api_key="x").search("chicken")
        top = items[0]
        assert top.source is FoodSource.USDA
        assert top.per100g.protein_g == 31.0 and top.per100g.kcal == 165.0
        assert top.external_id  # fdcId carried through

    def test_food_without_energy_is_dropped(self, usda_chicken_payload):
        # The fixture contains 3 foods; exactly one lacks nutrient 1008 (energy).
        items = USDAAdapter(FakeTransport(usda_chicken_payload), api_key="x").search("chicken")
        assert len(items) == 2
        assert all(i.external_id != "NO_ENERGY_FDCID" for i in items)
```

- [ ] **Step 2: Run test to verify it fails** — Expected: FAIL (import error).
- [ ] **Step 3: Create the fixture** from a real trimmed USDA response (include one food missing nutrient 1008), add a `usda_chicken_payload` fixture that loads it, and **implement `usda.py`** per the Interfaces block.
- [ ] **Step 4: Run test to verify it passes** — Expected: PASS.
- [ ] **Step 5: Commit** — `git commit -m "feat(nutrition): USDA FoodData Central adapter"`

---

### Task 6: Open Food Facts adapter

**Files:**
- Create: `nutrition_core/adapters/openfoodfacts.py`
- Create: `tests/fixtures/nutrition/off_product_kcal.json` and `tests/fixtures/nutrition/off_product_kj_only.json`
- Test: `tests/test_nutrition_adapter_off.py`

**Interfaces:**
- Consumes: `Transport`, `macros_from_fields`, `FoodAdapter` (Task 4); `FoodItem`, `FoodSource`, `Confidence` (Task 1).
- Produces:
  - `class OpenFoodFactsAdapter` with `__init__(self, transport: Transport)`, `search(self, query: str, *, limit: int = 5) -> list[FoodItem]`, and `get_by_barcode(self, barcode: str) -> FoodItem | None`. Reads `nutriments`: `energy-kcal_100g`→kcal; if absent, derive kcal from `energy-kj_100g` (or `energy_100g` when its unit is kJ) via `kcal = kJ / 4.184`; `proteins_100g`→protein_g, `carbohydrates_100g`→carbs_g, `fat_100g`→fat_g. Sets `source=FoodSource.OPEN_FOOD_FACTS`, `external_id=barcode/code`, `confidence=Confidence.MEDIUM`. A product with no derivable energy is omitted (search) or returns `None` (`get_by_barcode`).

- [ ] **Step 1: Write the failing test**

```python
class TestOpenFoodFactsAdapter:
    def test_reads_kcal_product(self, off_kcal_payload):
        # Fixture's nutriments has energy-kcal_100g = 250, proteins_100g = 12.
        item = OpenFoodFactsAdapter(FakeTransport(off_kcal_payload)).get_by_barcode("737628064502")
        assert item.source is FoodSource.OPEN_FOOD_FACTS
        assert item.per100g.kcal == 250.0 and item.per100g.protein_g == 12.0

    def test_converts_kj_only_product(self, off_kj_only_payload):
        item = OpenFoodFactsAdapter(FakeTransport(off_kj_only_payload)).get_by_barcode("0000000000000")
        # fixture has energy-kj_100g = 1000 -> 1000 / 4.184 ≈ 239.0 kcal
        assert item.per100g.kcal == pytest.approx(239.0, abs=0.5)

    def test_product_without_energy_returns_none(self, off_no_energy_payload):
        assert OpenFoodFactsAdapter(FakeTransport(off_no_energy_payload)).get_by_barcode("x") is None
```

- [ ] **Step 2: Run test to verify it fails** — Expected: FAIL (import error).
- [ ] **Step 3: Create the fixtures** (a kcal product, a kJ-only product with `energy-kj_100g: 1000`, and a no-energy product — the last can be a trimmed variant), add the loader fixtures, and **implement `openfoodfacts.py`** per the Interfaces block.
- [ ] **Step 4: Run test to verify it passes** — Expected: PASS.
- [ ] **Step 5: Commit** — `git commit -m "feat(nutrition): Open Food Facts adapter with kJ->kcal fallback"`

---

## Done when

All six tasks are committed and `pytest tests/test_nutrition_*.py -v` is green. `nutrition_core` can look up a food (USDA or OFF) and produce verified, deterministically-computed, practically-rounded meal macros with a budget/corner assessment — with no LLM, network, or database involved. The next plan adds persistence (the abstract connection interface + the `MealStore`→libSQL embedded-replica migration).
