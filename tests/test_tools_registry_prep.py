import sqlite3

from jarvisd.tools import build_registry
from tests.test_tools_registry import FakeHA, FakeSystem
from tests.test_tools_registry_food import FakeFoodService
from tests.test_tools_registry_pantry import FakePantryStore

NEW = ("plan_prep", "cook_meal", "list_planned_meals")


def _names(tools):
    return {t["name"] for t in tools}


def _build(**kw):
    return build_registry(
        FakeHA(), FakeSystem(),
        food_service=FakeFoodService(), pantry_store=FakePantryStore(),
        meal_log_conn=sqlite3.connect(":memory:"), **kw,
    )


class TestRegistryPrep:
    def test_registers_prep_tools(self):
        tools, handlers = _build()
        for n in NEW:
            assert n in handlers
            assert n in _names(tools)

    def test_default_build_omits(self):
        tools, handlers = build_registry(FakeHA(), FakeSystem())
        for n in NEW:
            assert n not in handlers
            assert n not in _names(tools)

    def test_requires_full_trio(self):
        conn = sqlite3.connect(":memory:")
        _, h = build_registry(FakeHA(), FakeSystem(), meal_log_conn=conn)
        assert not any(n in h for n in NEW)
        _, h = build_registry(FakeHA(), FakeSystem(), meal_log_conn=conn,
                              food_service=FakeFoodService())
        assert not any(n in h for n in NEW)
        _, h = build_registry(FakeHA(), FakeSystem(), meal_log_conn=conn,
                              pantry_store=FakePantryStore())
        assert not any(n in h for n in NEW)

    def test_does_not_drop_other_tools(self):
        tools, handlers = _build()
        for n in ("lookup_food", "pantry_list", "log_meal", "check_dish", "suggest_meals"):
            assert n in handlers
            assert n in _names(tools)
