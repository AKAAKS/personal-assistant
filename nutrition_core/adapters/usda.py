"""USDA FoodData Central adapter (/foods/search)."""

from __future__ import annotations

from nutrition_core.adapters.base import Transport, macros_from_fields
from nutrition_core.models import Confidence, FoodItem, FoodSource

SEARCH_URL = "https://api.nal.usda.gov/fdc/v1/foods/search"

# USDA nutrient numbers -> Macros fields (values are per 100g).
NUTRIENT_KCAL = 1008
NUTRIENT_PROTEIN = 1003
NUTRIENT_CARBS = 1005
NUTRIENT_FAT = 1004


class USDAAdapter:
    def __init__(self, transport: Transport, api_key: str) -> None:
        self._transport = transport
        self._api_key = api_key

    def search(self, query: str, *, limit: int = 5) -> list[FoodItem]:
        payload = self._transport.get_json(
            SEARCH_URL,
            {"query": query, "pageSize": limit, "api_key": self._api_key},
        )
        items: list[FoodItem] = []
        for food in payload.get("foods", []):
            nutrients = _nutrient_values(food.get("foodNutrients", []))
            macros = macros_from_fields(
                nutrients.get(NUTRIENT_KCAL),
                nutrients.get(NUTRIENT_PROTEIN),
                nutrients.get(NUTRIENT_CARBS),
                nutrients.get(NUTRIENT_FAT),
            )
            if macros is None:
                continue
            items.append(
                FoodItem(
                    name=food.get("description", ""),
                    per100g=macros,
                    source=FoodSource.USDA,
                    external_id=str(food.get("fdcId")),
                    confidence=Confidence.HIGH,
                )
            )
        return items[:limit]


def _nutrient_values(nutrients: list[dict]) -> dict[int, float]:
    values: dict[int, float] = {}
    for entry in nutrients:
        number = entry.get("nutrientId")
        value = entry.get("value")
        if number is not None and value is not None:
            values[int(number)] = float(value)
    return values
