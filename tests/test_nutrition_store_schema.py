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
