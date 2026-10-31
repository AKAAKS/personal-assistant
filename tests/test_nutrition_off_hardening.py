import pytest

from nutrition_core.adapters.openfoodfacts import OpenFoodFactsAdapter
from nutrition_core.models import FoodSource


class FakeTransport:
    def __init__(self, payload):
        self._payload = payload

    def get_json(self, url, params=None):
        return self._payload


def _good_product(code="123", **nutr):
    nutriments = {
        "energy-kcal_100g": 165,
        "proteins_100g": 31,
        "carbohydrates_100g": 0,
        "fat_100g": 3.6,
    }
    nutriments.update(nutr)
    return {"code": code, "product_name": "Chicken", "nutriments": nutriments}


def _search(payload):
    return OpenFoodFactsAdapter(FakeTransport(payload)).search("q")


def _barcode(payload):
    return OpenFoodFactsAdapter(FakeTransport(payload)).get_by_barcode("123")


class TestSearchMalformed:
    @pytest.mark.parametrize(
        "payload",
        [None, [], "x", {"products": None}, {"products": {"a": 1}}, {"products": "abc"}, {}],
    )
    def test_bad_payload_shapes(self, payload):
        assert _search(payload) == []

    def test_product_entry_string_skipped(self):
        assert _search({"products": ["junk", 5, None]}) == []

    def test_bad_entry_dropped_good_kept(self):
        items = _search({"products": ["junk", _good_product()]})
        assert len(items) == 1

    def test_nutriments_not_dict(self):
        p = _good_product()
        p["nutriments"] = "bad"
        assert _search({"products": [p]}) == []

    def test_overflow_number_no_raise(self):
        p = _good_product(proteins_100g=10**400)
        items = _search({"products": [p]})
        assert all(i.per100g.protein_g == 0 for i in items)

    def test_valid_product_still_parsed(self):
        [item] = _search({"products": [_good_product()]})
        assert item.source == FoodSource.OPEN_FOOD_FACTS
        assert item.per100g.kcal == 165
        assert item.per100g.protein_g == 31
        assert item.external_id == "123"

    def test_kj_fallback_intact(self):
        p = _good_product()
        del p["nutriments"]["energy-kcal_100g"]
        p["nutriments"]["energy-kj_100g"] = 418.4
        [item] = _search({"products": [p]})
        assert item.per100g.kcal == pytest.approx(100)


class TestBarcodeMalformed:
    @pytest.mark.parametrize(
        "payload",
        [
            None,
            [],
            "x",
            {},
            {"product": _good_product()},
            {"status": 0, "product": _good_product()},
            {"status": 1, "product": "junk"},
            {"status": 1, "product": None},
            {"status": 1, "product": ["a"]},
        ],
    )
    def test_bad_payload_shapes(self, payload):
        assert _barcode(payload) is None

    def test_overflow_number_no_raise(self):
        p = _good_product(proteins_100g=10**400)
        item = _barcode({"status": 1, "product": p})
        assert item is None or item.per100g.protein_g == 0

    def test_valid_product_still_parsed(self):
        item = _barcode({"status": 1, "product": _good_product()})
        assert item.per100g.kcal == 165
        assert item.external_id == "123"
