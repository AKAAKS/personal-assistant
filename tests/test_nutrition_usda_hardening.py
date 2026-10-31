import pytest

from nutrition_core.adapters.usda import USDAAdapter
from nutrition_core.models import FoodSource


class FakeTransport:
    def __init__(self, payload):
        self._payload = payload

    def get_json(self, url, params=None):
        return self._payload


def _nutrients():
    return [
        {"nutrientId": 1008, "value": 165},
        {"nutrientId": 1003, "value": 31},
        {"nutrientId": 1005, "value": 0},
        {"nutrientId": 1004, "value": 3.6},
    ]


def _good_food(fdc=1, name="Chicken"):
    return {"fdcId": fdc, "description": name, "foodNutrients": _nutrients()}


def _search(payload):
    return USDAAdapter(FakeTransport(payload), api_key="x").search("q")


class TestMalformedPayloads:
    @pytest.mark.parametrize("payload", [None, [], "oops", 5])
    def test_non_dict_payload(self, payload):
        assert _search(payload) == []

    @pytest.mark.parametrize("foods", [None, {"a": 1}, "str", 3])
    def test_foods_not_a_list(self, foods):
        assert _search({"foods": foods}) == []

    def test_food_entry_is_string(self):
        items = _search({"foods": ["bad", None, 4, _good_food()]})
        assert len(items) == 1

    @pytest.mark.parametrize("fn", [None, {"a": 1}, "str"])
    def test_food_nutrients_not_a_list(self, fn):
        assert _search({"foods": [{"fdcId": 1, "description": "x", "foodNutrients": fn}]}) == []

    def test_nutrient_entry_is_string(self):
        food = _good_food()
        food["foodNutrients"] = ["junk", None] + food["foodNutrients"]
        assert len(_search({"foods": [food]})) == 1

    def test_nutrient_value_non_numeric_skipped(self):
        food = _good_food()
        food["foodNutrients"].append({"nutrientId": 1003, "value": "abc"})
        food["foodNutrients"].insert(0, {"nutrientId": 1005, "value": "abc"})
        assert len(_search({"foods": [food]})) == 1

    def test_energy_value_non_numeric_drops_food(self):
        food = {"fdcId": 1, "description": "x",
                "foodNutrients": [{"nutrientId": 1008, "value": "abc"}]}
        assert _search({"foods": [food]}) == []

    def test_nutrient_id_none_or_garbage_skipped(self):
        food = _good_food()
        food["foodNutrients"] += [{"nutrientId": None, "value": 5},
                                  {"nutrientId": "zz", "value": 5}]
        assert len(_search({"foods": [food]})) == 1


class TestNumericExtremes:
    def test_huge_int_value_overflow_dropped(self):
        food = {"fdcId": 1, "description": "x",
                "foodNutrients": [{"nutrientId": 1008, "value": 10**400}]}
        assert _search({"foods": [food]}) == []

    def test_huge_int_value_on_other_nutrient_skipped(self):
        food = _good_food()
        food["foodNutrients"].insert(0, {"nutrientId": 1005, "value": 10**400})
        assert len(_search({"foods": [food]})) == 1

    def test_infinite_nutrient_id_skipped(self):
        food = _good_food()
        food["foodNutrients"].insert(0, {"nutrientId": float("inf"), "value": 5})
        assert len(_search({"foods": [food]})) == 1

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
    def test_non_finite_energy_value_drops_food(self, bad):
        food = {"fdcId": 1, "description": "x",
                "foodNutrients": [{"nutrientId": 1008, "value": bad}]}
        assert _search({"foods": [food]}) == []


class TestValidPayload:
    def test_valid_payload_still_parsed(self):
        items = _search({"foods": [_good_food(42, "Chicken breast")]})
        assert len(items) == 1
        item = items[0]
        assert item.source is FoodSource.USDA
        assert item.name == "Chicken breast"
        assert item.external_id == "42"
        assert item.per100g.kcal == 165.0
        assert item.per100g.protein_g == 31.0
        assert item.per100g.carbs_g == 0.0
        assert item.per100g.fat_g == 3.6
