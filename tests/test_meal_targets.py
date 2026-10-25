"""Nutrition targets + trends: MealStore methods and the meals tools."""

from jarvisd import db
from jarvisd.meal_store import MealStore
from jarvisd.tools.meals import handlers


def _store():
    return MealStore(db.connect(":memory:"))


def test_set_and_get_targets():
    s = _store()
    out = handlers(s)["set_nutrition_targets"]({
        "calories": 2200, "protein_g": 150, "carbs_g": 200, "fat_g": 70,
    })
    assert "2200" in out
    t = s.get_targets()
    assert t["calories"] == 2200 and t["protein_g"] == 150


def test_set_targets_is_a_single_overwriting_row():
    s = _store()
    h = handlers(s)
    h["set_nutrition_targets"]({"calories": 2000, "protein_g": 120, "carbs_g": 180, "fat_g": 60})
    h["set_nutrition_targets"]({"calories": 2500, "protein_g": 160, "carbs_g": 220, "fat_g": 80})
    assert s.get_targets()["calories"] == 2500


def test_set_nutrition_targets_requires_numbers():
    out = handlers(_store())["set_nutrition_targets"]({"calories": "lots"})
    assert "error" in out.lower()


def test_meal_summary_shows_progress_vs_target():
    s = _store()
    h = handlers(s)
    h["set_nutrition_targets"]({"calories": 2000, "protein_g": 100, "carbs_g": 200, "fat_g": 60})
    s.add("eggs", "2", 300, 20, 2, 22, "high", "")
    out = h["meal_summary"]({})
    assert "2000" in out      # the target shows up
    assert "300" in out       # today's total


def test_meal_summary_without_targets_still_works():
    s = _store()
    s.add("eggs", "2", 300, 20, 2, 22, "high", "")
    out = handlers(s)["meal_summary"]({})
    assert "300" in out       # no target set -> still summarizes, no crash


def test_meal_trends_aggregates_recent_days():
    s = _store()
    s.add("lunch", "", 500, 10, 60, 15, "medium", "")
    out = handlers(s)["meal_trends"]({"days": 7})
    assert "500" in out


def test_meal_trends_empty():
    out = handlers(_store())["meal_trends"]({})
    assert "no" in out.lower()
