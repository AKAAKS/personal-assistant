import json
from pathlib import Path

import pytest

from nutrition_core.adapters.usda import USDAAdapter
from nutrition_core.models import FoodSource

FIXTURE = Path(__file__).parent / "fixtures" / "nutrition" / "usda_search_chicken.json"


@pytest.fixture
def usda_chicken_payload():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


class FakeTransport:
    def __init__(self, payload): self._payload = payload
    def get_json(self, url, params=None): return self._payload

class TestUSDAAdapter:
    def test_parses_foods_into_fooditems(self, usda_chicken_payload):
        items = USDAAdapter(FakeTransport(usda_chicken_payload), api_key="x").search("chicken")
        top = items[0]
        assert top.source is FoodSource.USDA
        assert top.per100g.protein_g == 31.0 and top.per100g.kcal == 165.0
        assert top.external_id  # fdcId carried through

    def test_food_without_energy_is_dropped(self, usda_chicken_payload):
        # The fixture contains 3 foods; exactly one lacks nutrient 1008 (energy).
        items = USDAAdapter(FakeTransport(usda_chicken_payload), api_key="x").search("chicken")
        assert len(items) == 2
        assert all(i.external_id != "NO_ENERGY_FDCID" for i in items)
