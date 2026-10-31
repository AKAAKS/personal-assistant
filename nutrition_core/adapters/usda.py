"""USDA FoodData Central adapter (/foods/search)."""

from __future__ import annotations

import math

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
        if not isinstance(payload, dict):
            payload = {}
        foods = payload.get("foods")
        if not isinstance(foods, list):
            foods = []
        items: list[FoodItem] = []
        for food in foods:
            if not isinstance(food, dict):
                continue
            nutrients = _nutrient_values(food.get("foodNutrients"))
            macros = macros_from_fields(
                nutrients.get(NUTRIENT_KCAL),
                nutrients.get(NUTRIENT_PROTEIN),
                nutrients.get(NUTRIENT_CARBS),
                nutrients.get(NUTRIENT_FAT),
            )
            if macros is None:
                continue
            description = food.get("description")
            items.append(
                FoodItem(
                    name=description if isinstance(description, str) else "",
                    per100g=macros,
                    source=FoodSource.USDA,
                    external_id=str(food.get("fdcId")),
                    confidence=Confidence.HIGH,
                )
            )
        return items[:limit]


def _nutrient_values(nutrients: object) -> dict[int, float]:
    values: dict[int, float] = {}
    if not isinstance(nutrients, list):
        return values
    for entry in nutrients:
        if not isinstance(entry, dict):
            continue
        number = entry.get("nutrientId")
        value = entry.get("value")
        if number is None or value is None:
            continue
        try:
            key = int(number)
            amount = float(value)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(amount):
            continue
        values[key] = amount
    return values
