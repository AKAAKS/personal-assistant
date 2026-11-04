from datetime import datetime

from jarvisd.db import connect
from jarvisd.meal_store import MealStore
from nutrition_core.models import FoodSource, MealIngredient, Macros
from nutrition_core.store.meals import log_verified_meal


def _plan(conn, day):
    log_verified_meal(conn, name="Plan", date=day, status="planned",
                      ingredients=[MealIngredient("Chicken", 100, Macros(165, 31, 0, 3.6), FoodSource.USDA)])


def test_planned_excluded_everywhere():
    conn = connect(":memory:")
    store = MealStore(conn)
    today = datetime.now().date().isoformat()
    _plan(conn, today)
    assert store.today_summary()["meals"] == []
    assert store.today_summary()["calories"] == 0
    assert store.logged_today() is False
    assert store.range_summary(7) == []


def test_eaten_still_counted_alongside_planned():
    conn = connect(":memory:")
    store = MealStore(conn)
    today = datetime.now().date().isoformat()
    _plan(conn, today)
    store.add("toast", "1", 100, 3, 20, 1, "high", "")
    assert len(store.today_summary()["meals"]) == 1
    assert store.logged_today() is True
    assert store.range_summary(7)[0]["meals"] == 1
