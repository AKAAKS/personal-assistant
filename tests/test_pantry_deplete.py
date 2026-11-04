"""Pantry depletion handles + epsilon floor on adjust_quantity."""

from jarvisd.db import connect
from jarvisd.pantry_store import PantryStore
from nutrition_core.models import Macros

PER100 = Macros(kcal=100, protein_g=10, carbs_g=10, fat_g=1)


def _store():
    return PantryStore(connect(":memory:"))


def _add(store, grams=100.0, external_id="rice"):
    return store.add_or_topup(
        name="Rice",
        grams=grams,
        per100g=PER100,
        source="usda",
        external_id=external_id,
        confidence="high",
    )


class TestEpsilon:
    def test_repeated_fractional_depletion_deletes_row(self):
        store = _store()
        pid = _add(store, grams=3.0)
        for _ in range(30):
            store.deplete_by_id(pid, 0.1)
        assert store.list_items() == []


class TestDeplete:
    def test_reduces_and_returns_remaining(self):
        store = _store()
        _add(store, grams=100.0)
        assert store.deplete(source="usda", external_id="rice", grams=30.0) == 70.0
        assert store.list_items()[0]["grams_available"] == 70.0

    def test_over_deplete_removes_row_returns_zero(self):
        store = _store()
        _add(store, grams=50.0)
        assert store.deplete(source="usda", external_id="rice", grams=80.0) == 0.0
        assert store.list_items() == []

    def test_not_in_pantry_returns_none(self):
        store = _store()
        _add(store, grams=50.0)
        assert store.deplete(source="usda", external_id="nope", grams=10.0) is None
        assert store.list_items()[0]["grams_available"] == 50.0


class TestDepleteById:
    def test_reduces_row(self):
        store = _store()
        pid = _add(store, grams=100.0)
        assert store.deplete_by_id(pid, 25.0) == 75.0
        assert store.list_items()[0]["grams_available"] == 75.0

    def test_over_deplete_removes_row(self):
        store = _store()
        pid = _add(store, grams=10.0)
        assert store.deplete_by_id(pid, 99.0) == 0.0
        assert store.list_items() == []
