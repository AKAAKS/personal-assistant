from jarvisd.db import connect
from nutrition_core.budget import FitAssessment
from nutrition_core.models import FoodSource, Macros, MealIngredient
from nutrition_core.store.goals import set_goals
from nutrition_core.store.meals import day_status, log_verified_meal

D = "2026-11-03"


def _egg(grams=100):
    return [MealIngredient("egg", grams, Macros(155, 13, 1.1, 11), FoodSource.USDA)]


class TestLogParams:
    def test_confidence_and_assumptions_persist(self):
        conn = connect(":memory:")
        mid = log_verified_meal(conn, name="x", date=D, ingredients=_egg(),
                                status="estimated", confidence="low", assumptions="eyeballed")
        row = conn.execute("SELECT * FROM meals WHERE id=?", (mid,)).fetchone()
        assert row["confidence"] == "low" and row["assumptions"] == "eyeballed"
        assert row["status"] == "estimated"

    def test_defaults_unchanged(self):
        conn = connect(":memory:")
        mid = log_verified_meal(conn, name="x", date=D, ingredients=_egg())
        row = conn.execute("SELECT * FROM meals WHERE id=?", (mid,)).fetchone()
        assert row["confidence"] == "high" and row["assumptions"] == ""


class TestDayStatus:
    def test_buckets_and_budget(self):
        conn = connect(":memory:")
        target = Macros(2000, 150, 200, 60)
        set_goals(conn, target)
        log_verified_meal(conn, name="egg", date=D, ingredients=_egg(100))
        log_verified_meal(conn, name="egg2", date=D, ingredients=_egg(50), status="estimated")
        s = day_status(conn, D)
        assert s["target"] == target
        assert s["verified"] == Macros(155, 13, 1.1, 11)
        assert s["estimated"] == Macros(77.5, 6.5, 0.55, 5.5)
        assert s["consumed"] == s["verified"] + s["estimated"]
        assert s["remaining"] == target - s["consumed"]
        assert isinstance(s["assessment"], FitAssessment)

    def test_no_goal(self):
        conn = connect(":memory:")
        log_verified_meal(conn, name="egg", date=D, ingredients=_egg(100))
        s = day_status(conn, D)
        assert s["target"] is None and s["remaining"] is None and s["assessment"] is None
        assert s["verified"] == Macros(155, 13, 1.1, 11)
        assert s["estimated"] == Macros(0, 0, 0, 0)
        assert s["consumed"] == s["verified"]

    def test_over_budget(self):
        conn = connect(":memory:")
        set_goals(conn, Macros(100, 10, 10, 10))
        log_verified_meal(conn, name="egg", date=D, ingredients=_egg(200))
        assert day_status(conn, D)["assessment"].status == "over"

    def test_legacy_estimate_row_is_estimated(self):
        conn = connect(":memory:")
        conn.execute(
            "INSERT INTO meals (ts, description, quantity, calories, protein_g, carbs_g, "
            "fat_g, confidence, assumptions, status, name) "
            "VALUES (?, 'pizza', '', 600, 25, 70, 25, 'low', '', 'estimated', 'pizza')",
            (D,),
        )
        conn.commit()
        s = day_status(conn, D)
        assert s["estimated"] == Macros(600, 25, 70, 25)
        assert s["verified"] == Macros(0, 0, 0, 0)
