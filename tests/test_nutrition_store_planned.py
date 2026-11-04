import pytest

from jarvisd.db import connect
from nutrition_core.models import FoodSource, Macros, MealIngredient
from nutrition_core.store.goals import set_goals
from nutrition_core.store.meals import (
    cook_planned_meal,
    day_status,
    day_totals,
    list_planned,
    log_verified_meal,
    remaining_today,
)

TODAY = "2026-11-03"
GOAL = Macros(2000, 150, 200, 60)


def _ing(name="rice", grams=100.0):
    return MealIngredient(name, grams, Macros(100, 10, 20, 1), FoodSource.MANUAL)


@pytest.fixture
def conn():
    c = connect(":memory:")
    set_goals(c, GOAL)
    return c


def test_planned_excluded_from_consumed(conn):
    log_verified_meal(conn, name="p", date=TODAY, ingredients=[_ing()], status="planned")
    assert day_totals(conn, TODAY) == Macros(0, 0, 0, 0)
    st = day_status(conn, TODAY)
    assert st["consumed"] == Macros(0, 0, 0, 0)
    assert st["verified"] == Macros(0, 0, 0, 0)
    assert remaining_today(conn, TODAY) == GOAL

    log_verified_meal(conn, name="v", date=TODAY, ingredients=[_ing()], status="verified")
    assert day_totals(conn, TODAY).kcal == 100
    assert day_status(conn, TODAY)["consumed"].kcal == 100
    assert remaining_today(conn, TODAY).kcal == 1900


def test_links_round_trip(conn):
    mid = log_verified_meal(
        conn, name="m", date=TODAY, ingredients=[_ing("a"), _ing("b")],
        links=[("off123", None), (None, 7)],
    )
    rows = conn.execute(
        "SELECT external_id, pantry_id FROM meal_ingredients WHERE meal_id=? ORDER BY id",
        (mid,),
    ).fetchall()
    assert [(r["external_id"], r["pantry_id"]) for r in rows] == [("off123", None), (None, 7)]


def test_links_default_null_and_length_check(conn):
    mid = log_verified_meal(conn, name="m", date=TODAY, ingredients=[_ing()])
    r = conn.execute("SELECT external_id, pantry_id FROM meal_ingredients WHERE meal_id=?", (mid,)).fetchone()
    assert r["external_id"] is None and r["pantry_id"] is None
    with pytest.raises(ValueError):
        log_verified_meal(conn, name="x", date=TODAY, ingredients=[_ing()], links=[])
    assert conn.execute("SELECT COUNT(*) c FROM meals").fetchone()["c"] == 1


def test_cook_planned_meal(conn):
    mid = log_verified_meal(
        conn, name="stew", date="2026-11-05", ingredients=[_ing("a"), _ing("b")],
        status="planned", links=[("off123", None), (None, 7)],
    )
    res = cook_planned_meal(conn, mid)
    assert res["ok"] is True
    assert res["name"] == "stew" and res["date"] == "2026-11-05"
    assert [(i["name"], i["external_id"], i["pantry_id"]) for i in res["ingredients"]] == [
        ("a", "off123", None), ("b", None, 7)]
    assert set(res["ingredients"][0]) == {"name", "grams", "source", "external_id", "pantry_id"}
    assert conn.execute("SELECT status FROM meals WHERE id=?", (mid,)).fetchone()["status"] == "verified"
    assert day_totals(conn, "2026-11-05").kcal == 200


def test_cook_rejects_non_planned_and_absent(conn):
    mid = log_verified_meal(conn, name="v", date=TODAY, ingredients=[_ing()], status="verified")
    r = cook_planned_meal(conn, mid)
    assert r["ok"] is False and r["reason"]
    assert conn.execute("SELECT status FROM meals WHERE id=?", (mid,)).fetchone()["status"] == "verified"
    r = cook_planned_meal(conn, 9999)
    assert r["ok"] is False and r["reason"]


def test_list_planned(conn):
    a = log_verified_meal(conn, name="a", date="2026-11-04", ingredients=[_ing()], status="planned")
    b = log_verified_meal(conn, name="b", date="2026-11-06", ingredients=[_ing(grams=200)], status="planned")
    log_verified_meal(conn, name="v", date="2026-11-05", ingredients=[_ing()], status="verified")
    res = list_planned(conn)
    assert [p["id"] for p in res] == [a, b]
    assert res[1]["macros"].kcal == 200 and res[0]["name"] == "a" and res[0]["date"] == "2026-11-04"
    assert [p["id"] for p in list_planned(conn, from_date="2026-11-05")] == [b]
