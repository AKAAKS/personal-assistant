from jarvisd.db import connect
from jarvisd.pantry_store import PantryStore
from jarvisd.tools import pantry as pantry_tools
from nutrition_core.food_service import FoodLookupError
from nutrition_core.models import Confidence, FoodItem, FoodSource, Macros

CHICKEN = FoodItem(
    name="Chicken breast, raw",
    per100g=Macros(120, 22.5, 0, 2.6),
    source=FoodSource.USDA,
    external_id="171077",
    confidence=Confidence.HIGH,
)
OATS = FoodItem(
    name="Oats",
    per100g=Macros(380, 13, 67, 7),
    source=FoodSource.OPEN_FOOD_FACTS,
    external_id="123",
    confidence=Confidence.MEDIUM,
)


class FakeFood:
    def __init__(self, barcode=None, results=None, error=False):
        self._barcode, self._results, self._error = barcode, results or [], error

    def by_barcode(self, barcode):
        if self._error:
            raise FoodLookupError("down")
        return self._barcode

    def lookup(self, query, *, limit=5):
        if self._error:
            raise FoodLookupError("down")
        return self._results


MANUAL = {
    "name": "Home granola", "grams": 300, "kcal_per_100g": 450,
    "protein_per_100g": 10, "carbs_per_100g": 60, "fat_per_100g": 18,
}


def make(food):
    store = PantryStore(connect(":memory:"))
    return store, pantry_tools.handlers(store, food)


class TestPantryAdd:
    def test_by_barcode(self):
        store, h = make(FakeFood(barcode=CHICKEN))
        out = h["pantry_add"]({"barcode": "999", "grams": 200})
        assert "Added 200 g of Chicken breast, raw" in out
        assert "usda:171077" in out
        (row,) = store.list_items()
        assert row["grams_available"] == 200
        assert row["source"] == "usda" and row["external_id"] == "171077"
        assert row["per100_kcal"] == 120 and row["confidence"] == "high"

    def test_by_name_uses_first(self):
        store, h = make(FakeFood(results=[CHICKEN, OATS]))
        out = h["pantry_add"]({"name": "chicken", "grams": 150})
        assert "Chicken breast, raw" in out
        (row,) = store.list_items()
        assert row["name"] == "Chicken breast, raw"

    def test_manual(self):
        store, h = make(FakeFood())
        out = h["pantry_add"](dict(MANUAL))
        assert "Added 300 g of Home granola" in out
        (row,) = store.list_items()
        assert row["source"] == "manual" and row["confidence"] == "medium"
        assert row["per100_fat_g"] == 18

    def test_barcode_not_found(self):
        store, h = make(FakeFood(barcode=None))
        out = h["pantry_add"]({"barcode": "1", "grams": 100})
        assert "couldn't find" in out.lower()
        assert store.list_items() == []

    def test_name_not_found(self):
        store, h = make(FakeFood(results=[]))
        out = h["pantry_add"]({"name": "zzz", "grams": 100})
        assert "couldn't find" in out.lower()
        assert store.list_items() == []

    def test_bad_grams(self):
        store, h = make(FakeFood(barcode=CHICKEN, results=[CHICKEN]))
        for bad in (None, 0, -5, "abc", True, float("nan"), float("inf")):
            ti = {"barcode": "1"}
            if bad is not None:
                ti["grams"] = bad
            out = h["pantry_add"](ti)
            assert out.startswith("Error"), bad
        assert store.list_items() == []

    def test_no_mode(self):
        store, h = make(FakeFood())
        assert h["pantry_add"]({"grams": 100}).startswith("Error")
        assert store.list_items() == []

    def test_partial_manual_writes_nothing(self):
        store, h = make(FakeFood(results=[]))
        out = h["pantry_add"]({"name": "x", "grams": 10, "kcal_per_100g": 5})
        assert out.startswith("Error")
        assert store.list_items() == []

    def test_manual_bad_macros(self):
        store, h = make(FakeFood())
        assert h["pantry_add"](dict(MANUAL, protein_per_100g="lots")).startswith("Error")
        assert h["pantry_add"](dict(MANUAL, kcal_per_100g=-1)).startswith("Error")
        assert store.list_items() == []

    def test_food_service_none(self):
        store, h = make(None)
        assert "isn't available" in h["pantry_add"]({"barcode": "1", "grams": 100})
        assert "isn't available" in h["pantry_add"]({"name": "chicken", "grams": 100})
        assert store.list_items() == []
        h["pantry_add"](dict(MANUAL))
        assert len(store.list_items()) == 1

    def test_lookup_error(self):
        store, h = make(FakeFood(error=True))
        for ti in ({"barcode": "1", "grams": 5}, {"name": "x", "grams": 5}):
            assert "couldn't reach the food database" in h["pantry_add"](ti)
        assert store.list_items() == []

    def test_duplicate_tops_up(self):
        store, h = make(FakeFood(barcode=CHICKEN))
        h["pantry_add"]({"barcode": "1", "grams": 100})
        h["pantry_add"]({"barcode": "1", "grams": 50})
        (row,) = store.list_items()
        assert row["grams_available"] == 150

    def test_never_raises(self):
        class Boom:
            def add_or_topup(self, **kw):
                raise RuntimeError("db")

            def list_items(self):
                raise RuntimeError("db")

            def set_quantity(self, *a):
                raise RuntimeError("db")

            def remove(self, *a):
                raise RuntimeError("db")

        h = pantry_tools.handlers(Boom(), FakeFood(barcode=CHICKEN))
        assert isinstance(h["pantry_add"]({"barcode": "1", "grams": 5}), str)
        assert isinstance(h["pantry_list"]({}), str)
        assert isinstance(h["pantry_set_quantity"]({"pantry_id": 1, "grams": 5}), str)
        assert isinstance(h["pantry_remove"]({"pantry_id": 1}), str)
        assert isinstance(h["pantry_add"](None), str)


class TestPantryList:
    def test_empty(self):
        _, h = make(None)
        assert h["pantry_list"]({}) == "Your pantry is empty."

    def test_populated(self):
        store, h = make(FakeFood(barcode=CHICKEN))
        h["pantry_add"]({"barcode": "1", "grams": 200})
        assert "Chicken breast, raw — 200 g — usda" in h["pantry_list"]({})


class TestSetQuantity:
    def _one(self):
        store, h = make(FakeFood(barcode=CHICKEN))
        h["pantry_add"]({"barcode": "1", "grams": 200})
        return store, h, store.list_items()[0]["id"]

    def test_valid(self):
        store, h, pid = self._one()
        out = h["pantry_set_quantity"]({"pantry_id": pid, "grams": 80})
        assert "80" in out and not out.startswith("Error")
        assert store.list_items()[0]["grams_available"] == 80

    def test_negative_and_zero(self):
        store, h, pid = self._one()
        assert h["pantry_set_quantity"]({"pantry_id": pid, "grams": -1}).startswith("Error")
        out = h["pantry_set_quantity"]({"pantry_id": pid, "grams": 0})
        assert out.startswith("Error") and "pantry_remove" in out
        assert store.list_items()[0]["grams_available"] == 200

    def test_bad_id_or_missing_row(self):
        store, h, pid = self._one()
        assert h["pantry_set_quantity"]({"pantry_id": "x", "grams": 5}).startswith("Error")
        assert "No pantry item" in h["pantry_set_quantity"]({"pantry_id": 999, "grams": 5})


class TestRemove:
    def test_remove(self):
        store, h = make(FakeFood(barcode=CHICKEN))
        h["pantry_add"]({"barcode": "1", "grams": 200})
        pid = store.list_items()[0]["id"]
        assert "Removed" in h["pantry_remove"]({"pantry_id": pid})
        assert store.list_items() == []

    def test_bad_id(self):
        _, h = make(None)
        assert h["pantry_remove"]({}).startswith("Error")
