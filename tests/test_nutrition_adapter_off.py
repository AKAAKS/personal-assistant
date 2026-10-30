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
