import json
from pathlib import Path

import pytest

from nutrition_core.adapters.openfoodfacts import OpenFoodFactsAdapter
from nutrition_core.models import FoodSource

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "nutrition"


def _load(name):
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


@pytest.fixture
def off_kcal_payload():
    return _load("off_product_kcal.json")


@pytest.fixture
def off_kj_only_payload():
    return _load("off_product_kj_only.json")


@pytest.fixture
def off_no_energy_payload():
    return _load("off_product_no_energy.json")


class FakeTransport:
    def __init__(self, payload): self._payload = payload
    def get_json(self, url, params=None): return self._payload


class TestOpenFoodFactsAdapter:
    def test_reads_kcal_product(self, off_kcal_payload):
        # Fixture's nutriments has energy-kcal_100g = 250, proteins_100g = 12.
        item = OpenFoodFactsAdapter(FakeTransport(off_kcal_payload)).get_by_barcode("737628064502")
        assert item.source is FoodSource.OPEN_FOOD_FACTS
        assert item.per100g.kcal == 250.0 and item.per100g.protein_g == 12.0

    def test_converts_kj_only_product(self, off_kj_only_payload):
        item = OpenFoodFactsAdapter(FakeTransport(off_kj_only_payload)).get_by_barcode("0000000000000")
        # fixture has energy-kj_100g = 1000 -> 1000 / 4.184 ≈ 239.0 kcal
        assert item.per100g.kcal == pytest.approx(239.0, abs=0.5)

    def test_product_without_energy_returns_none(self, off_no_energy_payload):
        assert OpenFoodFactsAdapter(FakeTransport(off_no_energy_payload)).get_by_barcode("x") is None


def _product(code, nutriments):
    return {"code": code, "product_name": f"p{code}", "nutriments": nutriments}


class TestOpenFoodFactsHardening:
    @pytest.mark.parametrize("bad", ["nan", "inf", "-inf", float("nan"), float("inf")])
    def test_non_finite_energy_treated_as_missing(self, bad):
        payload = {"status": 1, "product": _product("1", {"energy-kcal_100g": bad})}
        assert OpenFoodFactsAdapter(FakeTransport(payload)).get_by_barcode("1") is None

    @pytest.mark.parametrize("bad", ["nan", "inf", float("nan")])
    def test_non_finite_macro_defaults_to_zero(self, bad):
        import math
        payload = {"status": 1, "product": _product("2", {
            "energy-kcal_100g": 100, "proteins_100g": bad, "fat_100g": bad, "carbohydrates_100g": bad})}
        item = OpenFoodFactsAdapter(FakeTransport(payload)).get_by_barcode("2")
        m = item.per100g
        assert all(math.isfinite(v) for v in (m.kcal, m.protein_g, m.carbs_g, m.fat_g))
        assert m.protein_g == 0.0 and m.fat_g == 0.0 and m.carbs_g == 0.0

    def test_search_keeps_only_valid_and_respects_limit(self):
        payload = {"products": [
            _product("a", {"energy-kcal_100g": 100}),
            _product("b", {}),
            _product("c", {"energy-kcal_100g": 200}),
        ]}
        adapter = OpenFoodFactsAdapter(FakeTransport(payload))
        items = adapter.search("x", limit=5)
        assert [i.external_id for i in items] == ["a", "c"]
        assert [i.external_id for i in adapter.search("x", limit=1)] == ["a"]

    def test_barcode_not_found_status_zero_returns_none(self):
        assert OpenFoodFactsAdapter(FakeTransport({"status": 0})).get_by_barcode("9") is None
