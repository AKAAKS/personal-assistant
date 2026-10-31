# Nutrition Persistence (Phase 2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give `nutrition_core` a persistence layer — a shared schema and repository functions for goals, verified meals, and daily totals — wired into JARVIS's existing SQLite database without breaking its meal subsystem.

**Architecture:** `nutrition_core` owns the schema (as SQL it exports) and a thin repository layer that reads/writes through an abstract connection Protocol — so it runs on JARVIS's local SQLite today and a libSQL embedded replica later with no code change. JARVIS's proven migration runner (`jarvisd/db.py`) executes the new schema as migration 7; JARVIS's `MealStore` keeps its public API but its targets move to the new `goals` table (with history). All existing tests stay green.

**Tech Stack:** Python 3.11, `sqlite3`, `typing.Protocol`, pytest. Reuses `nutrition_core` Phase 1 (`models`, `engine`, `budget`).

**Spec:** `docs/superpowers/specs/2026-08-26-macro-chef-design.md` — sections "Data model (single store)" and "Persistence: embedded replica + sync", and build-order step 2.

## Global Constraints

- **Language/version:** Python 3.11.
- **No LLM / no network inside `nutrition_core`.** This phase adds persistence; the DB connection is passed in, never opened by the repository.
- **Turso / libSQL embedded replica is OUT OF SCOPE** for this plan (needs credentials). Everything is built and tested against local SQLite. The connection Protocol is the seam that lets libSQL drop in later; do not add a libSQL dependency now.
- **`nutrition_core` owns the schema:** the DDL lives in `nutrition_core/store/schema.py` as an exported SQL string; `jarvisd/db.py` migration 7 executes that string. JARVIS defines no new table DDL of its own.
- **Backward compatibility is mandatory:** the existing `meals` and `nutrition_targets` tables and all current rows must keep working. Migration 7 only ADDs tables/columns and SEEDS; it never drops or rewrites existing data. The full existing suite (730 pass / 3 pre-existing port-8765 failures) stays green.
- **`Macros` remains the one numeric type** for goals/totals/remaining (Phase 1).
- **Tests:** pytest, class-based, in `tests/` (flat), one `tests/test_nutrition_store_*.py` per repo module; use an in-memory SQLite connection from `jarvisd.db.connect(":memory:")` (which runs migrations) as the fixture.
- **Dates/times:** store ISO-8601 strings (`datetime.now().isoformat(timespec="seconds")`), matching the existing `meals.ts` convention.

## Review Focus

- **Migration idempotence / re-run safety** (schema): applying migration 7 twice, or on a DB that already has data at version 6, must not error or duplicate-seed goals. → Task 1.
- **Empty/legacy DB seeding** (schema): a version-6 DB with NO `nutrition_targets` row must migrate cleanly (no goals seeded, not a crash); one WITH a row seeds exactly one active goal. → Task 1.
- **Goals history integrity** (goals repo): after N `set_goals` calls there is exactly one `active=1` row and N−1 inactive; `active_goals` returns the latest. → Task 4.
- **Per-100g snapshot immutability** (meals repo): a logged verified meal stores the per-100g values passed at log time, so later changes to source data cannot alter a logged meal's stored macros. → Task 5.
- **Remaining with no goal set** (meals repo): `remaining_today` when no active goal exists returns a clear sentinel (None), not a crash or a goal-of-zero. → Task 5.

---

### Task 1: Schema + migration 7

**Files:**
- Create: `nutrition_core/store/__init__.py`
- Create: `nutrition_core/store/schema.py`
- Modify: `jarvisd/db.py` (add migration 7, bump `SCHEMA_VERSION` 6→7)
- Test: `tests/test_nutrition_store_schema.py`, and extend `tests/test_db.py` if present (otherwise cover migration in the new file)

**Interfaces:**
- Consumes: nothing from Phase 1.
- Produces: `nutrition_core/store/schema.py` exporting `NEW_TABLES_SQL: str` (CREATE TABLE statements for `goals`, `meal_ingredients`, `pantry`, `disliked_foods`, `food_cache`, `settings`, plus `ALTER TABLE meals ADD COLUMN status TEXT NOT NULL DEFAULT 'estimated'` and `ALTER TABLE meals ADD COLUMN name TEXT`) and `SEED_GOALS_FROM_TARGETS_SQL: str` (INSERT into `goals` selecting the single `nutrition_targets` row when it exists). Column shapes:
  - `goals(id INTEGER PRIMARY KEY AUTOINCREMENT, kcal INTEGER NOT NULL, protein_g REAL NOT NULL, carbs_g REAL NOT NULL, fat_g REAL NOT NULL, effective_from TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1)`
  - `meal_ingredients(id INTEGER PRIMARY KEY AUTOINCREMENT, meal_id INTEGER NOT NULL REFERENCES meals(id), name TEXT NOT NULL, grams REAL NOT NULL, per100_kcal REAL NOT NULL, per100_protein_g REAL NOT NULL, per100_carbs_g REAL NOT NULL, per100_fat_g REAL NOT NULL, source TEXT NOT NULL, kcal REAL NOT NULL, protein_g REAL NOT NULL, carbs_g REAL NOT NULL, fat_g REAL NOT NULL)`
  - `pantry(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, source TEXT, external_id TEXT, grams_available REAL NOT NULL, updated_at TEXT NOT NULL)`
  - `disliked_foods(id INTEGER PRIMARY KEY AUTOINCREMENT, pattern TEXT NOT NULL, note TEXT NOT NULL DEFAULT '')`
  - `food_cache(source TEXT NOT NULL, external_id TEXT NOT NULL, name TEXT NOT NULL, per100_kcal REAL NOT NULL, per100_protein_g REAL NOT NULL, per100_carbs_g REAL NOT NULL, per100_fat_g REAL NOT NULL, fetched_at TEXT NOT NULL, PRIMARY KEY (source, external_id))`
  - `settings(key TEXT PRIMARY KEY, value TEXT NOT NULL)`
- In `jarvisd/db.py`: `MIGRATIONS[7]` runs `NEW_TABLES_SQL` then `SEED_GOALS_FROM_TARGETS_SQL`; `SCHEMA_VERSION = 7`. Import the SQL from `nutrition_core.store.schema` (JARVIS owns no new DDL text).

- [ ] **Step 1: Write the failing test**

```python
from jarvisd.db import connect

class TestMigration7:
    def test_new_tables_exist_after_migrate(self):
        conn = connect(":memory:")
        names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"goals", "meal_ingredients", "pantry", "disliked_foods", "food_cache", "settings"} <= names

    def test_meals_gains_status_and_name_columns_preserving_old(self):
        conn = connect(":memory:")
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(meals)")}
        assert {"status", "name"} <= cols and {"calories", "protein_g", "assumptions"} <= cols

    def test_seed_sql_creates_one_active_goal_from_targets_and_is_idempotent(self):
        from nutrition_core.store.schema import SEED_GOALS_FROM_TARGETS_SQL
        conn = connect(":memory:")  # fresh DB: goals empty, no targets row
        conn.execute("INSERT INTO nutrition_targets (id,calories,protein_g,carbs_g,fat_g) VALUES (1,2000,150,200,60)")
        conn.commit()
        conn.executescript(SEED_GOALS_FROM_TARGETS_SQL)  # first seed
        conn.executescript(SEED_GOALS_FROM_TARGETS_SQL)  # second run must NOT duplicate
        rows = conn.execute("SELECT kcal, active FROM goals").fetchall()
        assert len(rows) == 1 and rows[0]["kcal"] == 2000 and rows[0]["active"] == 1

    def test_seed_sql_no_targets_seeds_nothing(self):
        from nutrition_core.store.schema import SEED_GOALS_FROM_TARGETS_SQL
        conn = connect(":memory:")  # fresh DB has no nutrition_targets row
        conn.executescript(SEED_GOALS_FROM_TARGETS_SQL)
        assert conn.execute("SELECT COUNT(*) c FROM goals").fetchone()["c"] == 0
```

- [ ] **Step 2: Run test to verify it fails** — `pytest tests/test_nutrition_store_schema.py -v`; Expected: FAIL (new tables absent at version 6).
- [ ] **Step 3: Implement.** Put the DDL in `schema.py`. In `db.py`, add `MIGRATIONS[7]` = `NEW_TABLES_SQL`, and run `SEED_GOALS_FROM_TARGETS_SQL` which must be guarded: `INSERT INTO goals (kcal, protein_g, carbs_g, fat_g, effective_from, active) SELECT calories, protein_g, carbs_g, fat_g, <now>, 1 FROM nutrition_targets WHERE id=1 AND NOT EXISTS (SELECT 1 FROM goals)`. Bump `SCHEMA_VERSION=7`. Because the migration runner only applies versions above the stored one, re-running `connect()` never re-applies 7 — but the `NOT EXISTS` guard keeps the seed correct even if invoked directly. Use a fixed `effective_from` sourced in Python where the test needs determinism, else `datetime.now()`.
- [ ] **Step 4: Run tests to verify they pass** — also add/confirm a test that a targets-present DB seeds exactly one active goal and a targets-absent DB seeds none. Expected: PASS.
- [ ] **Step 5: Run the existing DB/meal suites to confirm green** — `pytest tests/test_db.py tests/test_meal_store.py tests/test_meal_targets.py -q` (adapt only if a test hard-codes `SCHEMA_VERSION`). Expected: PASS.
- [ ] **Step 6: Commit** — `git commit -m "feat(nutrition): migration 7 — shared schema (goals, meal_ingredients, pantry, cache)"`

---

### Task 2: Connection Protocol

**Files:**
- Create: `nutrition_core/store/connection.py`
- Test: `tests/test_nutrition_store_connection.py`

**Interfaces:**
- Produces: `class DbConnection(Protocol)` with the methods the repositories use: `def execute(self, sql: str, params: Sequence = ()) -> Any: ...`, `def executescript(self, sql: str) -> Any: ...`, `def commit(self) -> None: ...`. Document that `sqlite3.Connection` (and later a libSQL connection) satisfy it structurally, and that rows are accessed by column name (callers rely on `row_factory = sqlite3.Row`, which `jarvisd.db.connect` sets).

- [ ] **Step 1: Write the failing test**

```python
import sqlite3
from nutrition_core.store.connection import DbConnection

class TestDbConnection:
    def test_sqlite3_connection_satisfies_protocol(self):
        conn = sqlite3.connect(":memory:")
        assert isinstance(conn, DbConnection)  # requires @runtime_checkable
```

- [ ] **Step 2: Run test to verify it fails** — Expected: FAIL (import error).
- [ ] **Step 3: Implement `connection.py`** — a `@runtime_checkable` `Protocol` with the three methods above.
- [ ] **Step 4: Run test to verify it passes** — Expected: PASS.
- [ ] **Step 5: Commit** — `git commit -m "feat(nutrition): abstract DbConnection protocol (sqlite now, libSQL later)"`

---

### Task 3: `MealIngredient` model

**Files:**
- Modify: `nutrition_core/models.py`
- Test: extend `tests/test_nutrition_models.py`

**Interfaces:**
- Consumes: `Macros`, `FoodSource` (Phase 1).
- Produces: `@dataclass(frozen=True) class MealIngredient` with `name: str`, `grams: float`, `per100g: Macros`, `source: FoodSource` — the loggable (pre-persist) form of an ingredient, carrying the per-100g values to snapshot.

- [ ] **Step 1: Write the failing test**

```python
class TestMealIngredient:
    def test_holds_name_grams_per100g_source(self):
        mi = MealIngredient("egg", 100, Macros(155, 13, 1.1, 11), FoodSource.USDA)
        assert mi.grams == 100 and mi.per100g.protein_g == 13 and mi.source is FoodSource.USDA
```

- [ ] **Step 2: Run test to verify it fails** — Expected: FAIL.
- [ ] **Step 3: Implement** the frozen dataclass in `models.py`.
- [ ] **Step 4: Run test to verify it passes** — Expected: PASS.
- [ ] **Step 5: Commit** — `git commit -m "feat(nutrition): MealIngredient model (loggable per-100g form)"`

---

### Task 4: Goals repository

**Files:**
- Create: `nutrition_core/store/goals.py`
- Test: `tests/test_nutrition_store_goals.py`

**Interfaces:**
- Consumes: `Macros` (Phase 1), `DbConnection` (Task 2), the `goals` table (Task 1).
- Produces (in `nutrition_core/store/goals.py`):
  - `def set_goals(conn, goals: Macros, *, effective_from: str | None = None) -> int` — sets `active=0` on all existing rows, inserts a new `active=1` row (`kcal=int(goals.kcal)`), returns its id. `effective_from` defaults to now (ISO-8601).
  - `def active_goals(conn) -> Macros | None` — the single `active=1` row as `Macros`, or `None` if none.
  - `def goals_history(conn) -> list[Macros]` — all rows newest-first.

- [ ] **Step 1: Write the failing test**

```python
from jarvisd.db import connect
from nutrition_core.store.goals import set_goals, active_goals, goals_history

class TestGoalsRepo:
    def test_set_then_active_roundtrips(self):
        conn = connect(":memory:")
        set_goals(conn, Macros(2000, 150, 200, 60))
        assert active_goals(conn) == Macros(2000, 150, 200, 60)

    def test_new_goal_supersedes_old_single_active(self):
        conn = connect(":memory:")
        set_goals(conn, Macros(2000, 150, 200, 60))
        set_goals(conn, Macros(1800, 160, 150, 55))
        assert active_goals(conn) == Macros(1800, 160, 150, 55)
        n_active = conn.execute("SELECT COUNT(*) c FROM goals WHERE active=1").fetchone()["c"]
        assert n_active == 1 and len(goals_history(conn)) == 2

    def test_active_is_none_when_unset(self):
        assert active_goals(connect(":memory:")) is None
```

- [ ] **Step 2: Run test to verify it fails** — Expected: FAIL (import error).
- [ ] **Step 3: Implement `goals.py`** per the Interfaces block (one transaction per `set_goals`; deactivate-then-insert).
- [ ] **Step 4: Run test to verify it passes** — Expected: PASS.
- [ ] **Step 5: Commit** — `git commit -m "feat(nutrition): goals repository with history"`

---

### Task 5: Meals repository (verified logging + daily totals + remaining)

**Files:**
- Create: `nutrition_core/store/meals.py`
- Test: `tests/test_nutrition_store_meals.py`

**Interfaces:**
- Consumes: `Macros`, `MealIngredient` (Task 3), `ComputedIngredient`/`MealComputation` and `compute_ingredient`/`compute_meal` (Phase 1 engine), `budget.remaining` (Phase 1), `active_goals` (Task 4), the `meals`/`meal_ingredients` tables (Task 1), `DbConnection`.
- Produces (in `nutrition_core/store/meals.py`):
  - `def log_verified_meal(conn, *, name: str, date: str, ingredients: Sequence[MealIngredient], status: str = "eaten") -> int` — for each ingredient compute macros via `engine.compute_ingredient`; insert a `meals` row (status, name, date in `ts`, and the summed total into the existing `calories/protein_g/carbs_g/fat_g` columns so legacy summaries keep working, `confidence='high'`); insert one `meal_ingredients` row per ingredient storing BOTH the per-100g snapshot and the computed macros; return the meal id.
  - `def day_totals(conn, date: str) -> Macros` — sum of that date's meals (reuse the existing `ts LIKE date%` convention; all statuses count).
  - `def remaining_today(conn, date: str) -> Macros | None` — `budget.remaining(active_goals(conn), day_totals(conn, date))`, or `None` if no active goal.

- [ ] **Step 1: Write the failing test**

```python
from jarvisd.db import connect
from nutrition_core.store.goals import set_goals
from nutrition_core.store.meals import log_verified_meal, day_totals, remaining_today

class TestMealsRepo:
    def test_logs_and_totals_from_ingredients(self):
        conn = connect(":memory:")
        mid = log_verified_meal(conn, name="chicken & rice", date="2026-10-31",
            ingredients=[MealIngredient("chicken", 100, Macros(165,31,0,3.6), FoodSource.USDA),
                         MealIngredient("rice", 100, Macros(130,2.7,28,0.3), FoodSource.USDA)])
        assert isinstance(mid, int)
        assert day_totals(conn, "2026-10-31") == Macros(295, 33.7, 28, 3.9)

    def test_ingredient_snapshot_persisted(self):
        conn = connect(":memory:")
        log_verified_meal(conn, name="egg", date="2026-10-31",
            ingredients=[MealIngredient("egg", 50, Macros(155,13,1.1,11), FoodSource.USDA)])
        row = conn.execute("SELECT per100_kcal, grams, kcal FROM meal_ingredients").fetchone()
        assert row["per100_kcal"] == 155 and row["grams"] == 50 and row["kcal"] == 77.5

    def test_remaining_uses_goal_minus_eaten(self):
        conn = connect(":memory:")
        set_goals(conn, Macros(2000, 150, 200, 60))
        log_verified_meal(conn, name="egg", date="2026-10-31",
            ingredients=[MealIngredient("egg", 100, Macros(155,13,1.1,11), FoodSource.USDA)])
        assert remaining_today(conn, "2026-10-31") == Macros(1845, 137, 198.9, 49)

    def test_remaining_none_without_goal(self):
        conn = connect(":memory:")
        log_verified_meal(conn, name="egg", date="2026-10-31",
            ingredients=[MealIngredient("egg", 100, Macros(155,13,1.1,11), FoodSource.USDA)])
        assert remaining_today(conn, "2026-10-31") is None
```

- [ ] **Step 2: Run test to verify it fails** — Expected: FAIL (import error).
- [ ] **Step 3: Implement `meals.py`** per the Interfaces block. Store the meal total into the legacy `calories`(int)/`protein_g`/`carbs_g`/`fat_g` columns so JARVIS's existing `MealStore.today_summary`/`range_summary` keep working on verified meals. One transaction per logged meal.
- [ ] **Step 4: Run test to verify it passes** — Expected: PASS.
- [ ] **Step 5: Commit** — `git commit -m "feat(nutrition): meals repository (verified logging, daily totals, remaining)"`

---

### Task 6: MealStore integration — targets move to `goals`

**Files:**
- Modify: `jarvisd/meal_store.py` (`set_targets`/`get_targets`)
- Test: adapt `tests/test_meal_targets.py` (and confirm `tests/test_meal_store.py`, `tests/test_tools_meals.py` stay green)

**Interfaces:**
- Consumes: `nutrition_core.store.goals` (Task 4); `Macros`.
- Produces: `MealStore.set_targets(calories, protein_g, carbs_g, fat_g)` delegates to `goals.set_goals`; `MealStore.get_targets() -> dict | None` reads `goals.active_goals` and returns the same dict shape as before (`{"calories","protein_g","carbs_g","fat_g"}`) or `None`. Public signatures and return shapes are unchanged — only the backing store moves from the single-row `nutrition_targets` to `goals`.

- [ ] **Step 1: Write/adjust the failing test**

```python
class TestMealStoreTargetsOnGoals:
    def test_set_get_targets_roundtrip_via_goals(self, store, conn):
        store.set_targets(2000, 150, 200, 60)
        assert store.get_targets() == {"calories": 2000, "protein_g": 150.0, "carbs_g": 200.0, "fat_g": 60.0}
        # backed by the goals table now
        assert conn.execute("SELECT COUNT(*) c FROM goals WHERE active=1").fetchone()["c"] == 1

    def test_updating_targets_keeps_single_active(self, store, conn):
        store.set_targets(2000, 150, 200, 60)
        store.set_targets(1800, 160, 150, 55)
        assert store.get_targets()["calories"] == 1800
        assert conn.execute("SELECT COUNT(*) c FROM goals WHERE active=1").fetchone()["c"] == 1
```

- [ ] **Step 2: Run test to verify it fails** — Expected: FAIL (still reads `nutrition_targets`).
- [ ] **Step 3: Implement** the delegation in `meal_store.py`. Keep `nutrition_targets` table in place (migration seeds goals from it; no drop). Do not change any other `MealStore` method.
- [ ] **Step 4: Run the meal suites to verify green** — `pytest tests/test_meal_store.py tests/test_meal_targets.py tests/test_tools_meals.py -v`. Expected: PASS (adapt only assertions that peek at the `nutrition_targets` table directly).
- [ ] **Step 5: Commit** — `git commit -m "refactor(jarvis): MealStore targets backed by nutrition_core goals table"`

---

## Done when

All six tasks are committed; `pytest tests/test_nutrition_*.py tests/test_meal_store.py tests/test_meal_targets.py tests/test_tools_meals.py tests/test_db.py -q` is green, and the full suite shows no new failures beyond the pre-existing port-8765 server tests. `nutrition_core` can persist goals (with history) and verified meals (with per-100g snapshots) and compute remaining-today, all through an abstract connection, on JARVIS's existing database. The next plan wires the libSQL embedded replica (needs Turso credentials) and adds the JARVIS tool-pack.
