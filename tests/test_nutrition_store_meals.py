from jarvisd.db import connect
from nutrition_core.models import FoodSource, Macros, MealIngredient
from nutrition_core.store.goals import set_goals
from nutrition_core.store.meals import day_totals, log_verified_meal, remaining_today


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


class TestMealsRepoExtra:
    def test_legacy_columns_backfilled_and_status_name(self):
        conn = connect(":memory:")
        mid = log_verified_meal(conn, name="egg", date="2026-10-31",
            ingredients=[MealIngredient("egg", 50, Macros(155,13,1.1,11), FoodSource.USDA)])
        row = conn.execute("SELECT * FROM meals WHERE id=?", (mid,)).fetchone()
        assert row["calories"] == 78  # 77.5 rounds half up
        assert row["protein_g"] == 6.5 and row["carbs_g"] == 0.55 and row["fat_g"] == 5.5
        assert row["status"] == "eaten" and row["name"] == "egg"
        assert row["ts"].startswith("2026-10-31") and row["confidence"] == "high"
        ing = conn.execute("SELECT * FROM meal_ingredients WHERE meal_id=?", (mid,)).fetchone()
        assert (ing["per100_protein_g"], ing["per100_carbs_g"], ing["per100_fat_g"]) == (13, 1.1, 11)
        assert (ing["protein_g"], ing["carbs_g"], ing["fat_g"]) == (6.5, 0.55, 5.5)
        assert ing["source"] == FoodSource.USDA.value

    def test_legacy_meal_store_summary_sees_verified_meal(self):
        from jarvisd.meal_store import MealStore
        from datetime import datetime
        conn = connect(":memory:")
        log_verified_meal(conn, name="egg", date="2026-10-31",
            ingredients=[MealIngredient("egg", 100, Macros(155,13,1.1,11), FoodSource.USDA)])
        s = MealStore(conn).today_summary(now=datetime(2026, 10, 31, 12))
        assert s["calories"] == 155 and s["protein_g"] == 13 and len(s["meals"]) == 1

    def test_empty_day_is_zero(self):
        conn = connect(":memory:")
        assert day_totals(conn, "2026-10-31") == Macros(0, 0, 0, 0)

    def test_other_days_excluded_and_legacy_meals_counted(self):
        conn = connect(":memory:")
        log_verified_meal(conn, name="egg", date="2026-10-30",
            ingredients=[MealIngredient("egg", 100, Macros(155,13,1.1,11), FoodSource.USDA)])
        conn.execute("INSERT INTO meals (ts, description, calories, protein_g, carbs_g, fat_g) "
                     "VALUES ('2026-10-31T08:00:00', 'toast', 100, 3, 18, 1)")
        conn.commit()
        assert day_totals(conn, "2026-10-31") == Macros(100, 3, 18, 1)
