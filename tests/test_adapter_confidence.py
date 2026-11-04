"""Foods with a PRESENT-but-untrustworthy macro are dropped; ABSENT macros stay 0.0."""

import pytest

from nutrition_core.adapters.openfoodfacts import OpenFoodFactsAdapter
from nutrition_core.adapters.usda import USDAAdapter


class FakeTransport:
    def __init__(self, payload):
        self._payload = payload

    def get_json(self, url, params=None):
        return self._payload


def _usda(nutrients):
    food = {"fdcId": 1, "description": "x", "foodNutrients": nutrients}
    return USDAAdapter(FakeTransport({"foods": [food]}), api_key="k").search("q")


def _usda_nutrients(**over):
    base = {1008: 165, 1003: 31, 1005: 0, 1004: 3.6}
    base.update({int(k[1:]): v for k, v in over.items()})
    return [{"nutrientId": k, "value": v} for k, v in base.items() if v is not ...]


def _off_nutriments(**over):
    base = {"energy-kcal_100g": 165, "proteins_100g": 31,
            "carbohydrates_100g": 0, "fat_100g": 3.6}
    base.update(over)
    return {k: v for k, v in base.items() if v is not ...}


def _off_product(**over):
    return {"code": "123", "product_name": "x", "nutriments": _off_nutriments(**over)}


def _off_search(**over):
    adapter = OpenFoodFactsAdapter(FakeTransport({"products": [_off_product(**over)]}))
    return adapter.search("q")


def _off_barcode(**over):
    adapter = OpenFoodFactsAdapter(
        FakeTransport({"status": 1, "product": _off_product(**over)})
    )
    item = adapter.get_by_barcode("123")
    return [] if item is None else [item]


OFF_FIELDS = {"protein": "proteins_100g", "carbs": "carbohydrates_100g", "fat": "fat_100g"}
USDA_IDS = {"protein": "n1003", "carbs": "n1005", "fat": "n1004"}
BAD_VALUES = ["abc", None, "", float("nan"), float("inf"), -1, -0.5, 100.5, 1000]


class TestOpenFoodFacts:
    @pytest.mark.parametrize("fn", [_off_search, _off_barcode])
    @pytest.mark.parametrize("macro", OFF_FIELDS)
    @pytest.mark.parametrize("bad", BAD_VALUES)
    def test_present_but_bad_macro_dropped(self, fn, macro, bad):
        assert fn(**{OFF_FIELDS[macro]: bad}) == []

    @pytest.mark.parametrize("fn", [_off_search, _off_barcode])
    @pytest.mark.parametrize("kcal", [-1, 1000.5, 5000])
    def test_bad_kcal_dropped(self, fn, kcal):
        assert fn(**{"energy-kcal_100g": kcal}) == []

    @pytest.mark.parametrize("fn", [_off_search, _off_barcode])
    @pytest.mark.parametrize("macro", OFF_FIELDS)
    def test_absent_macro_kept_as_zero(self, fn, macro):
        [item] = fn(**{OFF_FIELDS[macro]: ...})
        got = {"protein": item.per100g.protein_g, "carbs": item.per100g.carbs_g,
               "fat": item.per100g.fat_g}
        assert got[macro] == 0.0

    def test_boundaries_kept(self):
        [item] = _off_search(**{"energy-kcal_100g": 1000, "proteins_100g": 100,
                                "carbohydrates_100g": 0, "fat_100g": 0})
        assert item.per100g.kcal == 1000 and item.per100g.protein_g == 100

    def test_absent_energy_dropped(self):
        assert _off_search(**{"energy-kcal_100g": ...}) == []

    def test_valid_unchanged(self):
        [item] = _off_search()
        m = item.per100g
        assert (m.kcal, m.protein_g, m.carbs_g, m.fat_g) == (165, 31, 0, 3.6)


class TestUSDA:
    @pytest.mark.parametrize("macro", USDA_IDS)
    @pytest.mark.parametrize("bad", BAD_VALUES)
    def test_present_but_bad_macro_dropped(self, macro, bad):
        assert _usda(_usda_nutrients(**{USDA_IDS[macro]: bad})) == []

    @pytest.mark.parametrize("kcal", [-1, 1000.5, 5000])
    def test_bad_kcal_dropped(self, kcal):
        assert _usda(_usda_nutrients(n1008=kcal)) == []

    @pytest.mark.parametrize("macro", USDA_IDS)
    def test_absent_macro_kept_as_zero(self, macro):
        [item] = _usda(_usda_nutrients(**{USDA_IDS[macro]: ...}))
        got = {"protein": item.per100g.protein_g, "carbs": item.per100g.carbs_g,
               "fat": item.per100g.fat_g}
        assert got[macro] == 0.0

    def test_absent_energy_dropped(self):
        assert _usda(_usda_nutrients(n1008=...)) == []

    def test_valid_unchanged(self):
        [item] = _usda(_usda_nutrients())
        m = item.per100g
        assert (m.kcal, m.protein_g, m.carbs_g, m.fat_g) == (165, 31, 0, 3.6)
