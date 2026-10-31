import pytest

from jarvisd.db import connect
from nutrition_core.food_service import FoodLookupError, FoodService
from nutrition_core.models import Confidence, FoodItem, FoodSource, Macros
from nutrition_core.store import food_cache


def _item(name, src, eid):
    return FoodItem(name, Macros(100, 10, 20, 5), src, eid, Confidence.HIGH)


class Fake:
    def __init__(self, items=None, exc=None):
        self.items, self.exc, self.calls = items or [], exc, 0

    def search(self, query, *, limit=5):
        self.calls += 1
        if self.exc:
            raise self.exc
        return list(self.items)


U = _item("Rice", FoodSource.USDA, "1")
O = _item("Rice bar", FoodSource.OPEN_FOOD_FACTS, "9")


class TestFoodService:
    def setup_method(self):
        self.conn = connect(":memory:")

    def test_lookup_combines_and_caches(self):
        res = FoodService(self.conn, usda=Fake([U]), off=Fake([O])).lookup("rice")
        assert res == [U, O]
        assert food_cache.get_cached(self.conn, "usda", "1").name == "Rice"
        assert food_cache.get_cached(self.conn, "open_food_facts", "9").name == "Rice bar"

    def test_lookup_off_only_when_no_usda(self):
        off = Fake([O])
        assert FoodService(self.conn, off=off).lookup("rice") == [O]
        assert off.calls == 1

    def test_one_adapter_failure_still_returns_other(self):
        svc = FoodService(self.conn, usda=Fake(exc=RuntimeError("boom")), off=Fake([O]))
        assert svc.lookup("rice") == [O]

    def test_all_adapters_fail_raises_foodlookuperror(self):
        svc = FoodService(self.conn, usda=Fake(exc=RuntimeError("a")), off=Fake(exc=OSError("b")))
        with pytest.raises(FoodLookupError):
            svc.lookup("rice")

    def test_no_matches_is_empty_not_error(self):
        assert FoodService(self.conn, usda=Fake([]), off=Fake([])).lookup("zzz") == []

    def test_limit_truncates_combined(self):
        svc = FoodService(self.conn, usda=Fake([U]), off=Fake([O]))
        assert svc.lookup("rice", limit=1) == [U]
