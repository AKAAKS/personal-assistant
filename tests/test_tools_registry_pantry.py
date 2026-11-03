from jarvisd.tools import build_registry
from tests.test_tools_registry import FakeHA, FakeSystem
from tests.test_tools_registry_food import FakeFoodService


class FakePantryStore:
    def list(self, *args, **kwargs):
        return []


def _names(tools):
    return {t["name"] for t in tools}


class TestRegistryPantry:
    def test_registry_includes_pantry_tools_when_store_present(self):
        tools, handlers = build_registry(
            FakeHA(), FakeSystem(),
            pantry_store=FakePantryStore(), food_service=FakeFoodService(),
        )
        for name in ("pantry_add", "pantry_list"):
            assert name in handlers
            assert name in _names(tools)

    def test_registry_omits_pantry_by_default(self):
        tools, handlers = build_registry(FakeHA(), FakeSystem())
        for name in ("pantry_add", "pantry_list"):
            assert name not in handlers
            assert name not in _names(tools)

    def test_pantry_does_not_remove_food_tools(self):
        tools, handlers = build_registry(
            FakeHA(), FakeSystem(),
            pantry_store=FakePantryStore(), food_service=FakeFoodService(),
        )
        assert "lookup_food" in handlers
        assert "lookup_food" in _names(tools)

    def test_pantry_registers_without_food_service(self):
        tools, handlers = build_registry(
            FakeHA(), FakeSystem(), pantry_store=FakePantryStore(),
        )
        assert "pantry_add" in handlers
        assert "lookup_food" not in handlers
