from datetime import date

from jarvisd.db import connect
from jarvisd.tools import meal_log
from nutrition_core.models import FoodSource, Macros, MealIngredient
from nutrition_core.store.goals import set_goals
from nutrition_core.store.meals import log_verified_meal

P100 = Macros(100, 10, 10, 5)


def _log(conn, kcal_grams, status):
    log_verified_meal(conn, name="m", date=date.today().isoformat(),
                      ingredients=[MealIngredient("f", kcal_grams, P100, FoodSource.USDA)],
                      status=status, confidence="high" if status != "estimated" else "low")


def _h(conn):
    return meal_log.handlers(conn, None, None)["day_status"]


def test_tools_registered():
    assert [t["name"] for t in meal_log.TOOLS] == ["log_meal", "log_estimated_meal", "day_status"]


def test_target_with_split_and_remaining():
    conn = connect(":memory:")
    set_goals(conn, Macros(2000, 150, 200, 60))
    _log(conn, 1400, "verified")   # 1400 kcal
    _log(conn, 250, "estimated")   # 250 kcal
    out = _h(conn)({})
    assert "2,000" in out
    assert "1,650 kcal (1,400 verified + 250 estimated)" in out
    assert "350" in out and "emaining" in out


def test_no_target():
    conn = connect(":memory:")
    _log(conn, 300, "verified")
    out = _h(conn)({})
    assert "No daily target set" in out
    assert "300 verified + 0 estimated" in out


def test_over_budget_line():
    conn = connect(":memory:")
    set_goals(conn, Macros(1000, 100, 100, 30))
    _log(conn, 1500, "verified")
    out = _h(conn)({})
    assert "over" in out.lower()


def test_empty_day():
    conn = connect(":memory:")
    assert "Nothing logged yet today" in _h(conn)({})


def test_never_raises():
    conn = connect(":memory:")
    h = _h(conn)
    for bad in (None, "x", {"date": 5}, {"date": "not-a-date"}):
        assert isinstance(h(bad), str)
    conn.close()
    assert isinstance(h({}), str)
