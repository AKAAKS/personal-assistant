from jarvisd.tools import build_registry
from tests.test_tools_registry import FakeHA, FakeSystem


class FakeFoodService:
    def lookup(self, *args, **kwargs):
        return []


class TestRegistryFood:
    def test_registry_includes_lookup_food_when_service_present(self):
        tools, handlers = build_registry(FakeHA(), FakeSystem(), food_service=FakeFoodService())
        assert "lookup_food" in handlers
        assert any(t["name"] == "lookup_food" for t in tools)

    def test_registry_omits_food_when_service_none(self):
        tools, handlers = build_registry(FakeHA(), FakeSystem())
        assert "lookup_food" not in handlers
        assert not any(t["name"] == "lookup_food" for t in tools)
