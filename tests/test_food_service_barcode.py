import pytest

from jarvisd.db import connect
from nutrition_core import food_service as fs_mod
from nutrition_core.food_service import FoodLookupError, FoodService
from nutrition_core.models import Confidence, FoodItem, FoodSource, Macros
from nutrition_core.store import food_cache


def _item(eid="123"):
    return FoodItem("Oats", Macros(100, 10, 20, 5), FoodSource.OPEN_FOOD_FACTS, eid, Confidence.HIGH)


class FakeOff:
    def __init__(self, item=None, exc=None):
        self.item, self.exc, self.calls = item, exc, []

    def get_by_barcode(self, barcode):
        self.calls.append(barcode)
        if self.exc:
            raise self.exc
        return self.item


class TestByBarcode:
    def test_hit_returns_and_caches(self):
        conn = connect(":memory:")
        off = FakeOff(_item("123"))
        got = FoodService(conn, off=off).by_barcode("123")
        assert got == _item("123")
        assert off.calls == ["123"]
        assert food_cache.get_cached(conn, FoodSource.OPEN_FOOD_FACTS, "123") is not None

    def test_miss_returns_none_nothing_cached(self):
        conn = connect(":memory:")
        assert FoodService(conn, off=FakeOff(None)).by_barcode("999") is None
        assert food_cache.get_cached(conn, FoodSource.OPEN_FOOD_FACTS, "999") is None

    def test_no_off_returns_none(self):
        assert FoodService(connect(":memory:")).by_barcode("123") is None

    def test_adapter_error_raises_lookup_error(self):
        svc = FoodService(connect(":memory:"), off=FakeOff(exc=RuntimeError("net")))
        with pytest.raises(FoodLookupError):
            svc.by_barcode("123")

    def test_cache_failure_still_returns_hit(self, monkeypatch):
        def boom(conn, item):
            raise RuntimeError("disk")

        monkeypatch.setattr(fs_mod, "cache_food", boom)
        got = FoodService(connect(":memory:"), off=FakeOff(_item())).by_barcode("123")
        assert got == _item()
