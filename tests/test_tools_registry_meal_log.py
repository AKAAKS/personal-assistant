import sqlite3

from jarvisd.tools import build_registry, meals
from tests.test_tools_registry import FakeHA, FakeSystem
from tests.test_tools_registry_food import FakeFoodService
from tests.test_tools_registry_pantry import FakePantryStore

NEW = ("log_meal", "log_estimated_meal", "day_status")


def _names(tools):
    return {t["name"] for t in tools}


def _build(**kw):
    return build_registry(
        FakeHA(), FakeSystem(),
        food_service=FakeFoodService(), pantry_store=FakePantryStore(),
        meal_log_conn=sqlite3.connect(":memory:"), **kw,
    )


class TestRegistryMealLog:
    def test_registers_meal_log_tools(self):
        tools, handlers = _build()
        for n in NEW:
            assert n in handlers
            assert n in _names(tools)

    def test_log_meal_is_new_db_sourced_tool(self):
        tools, _ = _build()
        d = next(t for t in tools if t["name"] == "log_meal")
        req = d["input_schema"]["required"]
        assert "ingredients" in req
        assert "calories" not in req

    def test_default_build_omits(self):
        tools, handlers = build_registry(FakeHA(), FakeSystem())
        for n in NEW:
            assert n not in handlers
            assert n not in _names(tools)

    def test_requires_pantry_and_food(self):
        conn = sqlite3.connect(":memory:")
        _, h = build_registry(FakeHA(), FakeSystem(), meal_log_conn=conn)
        assert "day_status" not in h
        _, h = build_registry(FakeHA(), FakeSystem(), meal_log_conn=conn,
                              food_service=FakeFoodService())
        assert "day_status" not in h

    def test_does_not_drop_food_or_pantry(self):
        tools, handlers = _build()
        for n in ("lookup_food", "pantry_add", "pantry_list"):
            assert n in handlers
            assert n in _names(tools)

    def test_legacy_log_meal_gone_from_meals(self):
        assert "log_meal" not in {t["name"] for t in meals.TOOLS}
