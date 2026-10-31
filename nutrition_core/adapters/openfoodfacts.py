"""Open Food Facts adapter (product lookup by barcode, and search)."""

from __future__ import annotations

import math

from nutrition_core.adapters.base import Transport, macros_from_fields
from nutrition_core.models import Confidence, FoodItem, FoodSource

PRODUCT_URL = "https://world.openfoodfacts.org/api/v2/product/{barcode}"
SEARCH_URL = "https://world.openfoodfacts.org/cgi/search.pl"

KJ_PER_KCAL = 4.184


class OpenFoodFactsAdapter:
    def __init__(self, transport: Transport) -> None:
        self._transport = transport

    def search(self, query: str, *, limit: int = 5) -> list[FoodItem]:
        payload = self._transport.get_json(
            SEARCH_URL,
            {
                "search_terms": query,
                "search_simple": 1,
                "action": "process",
                "json": 1,
                "page_size": limit,
            },
        )
        items: list[FoodItem] = []
        for product in payload.get("products", []):
            item = _to_food_item(product)
            if item is not None:
                items.append(item)
        return items[:limit]

    def get_by_barcode(self, barcode: str) -> FoodItem | None:
        payload = self._transport.get_json(PRODUCT_URL.format(barcode=barcode), None)
        if payload.get("status") != 1:
            return None
        product = payload.get("product")
        if not product:
            return None
        return _to_food_item(product, fallback_code=barcode)


def _to_food_item(product: dict, fallback_code: str | None = None) -> FoodItem | None:
    nutriments = product.get("nutriments", {}) or {}
    macros = macros_from_fields(
        _kcal(nutriments, product),
        _number(nutriments.get("proteins_100g")),
        _number(nutriments.get("carbohydrates_100g")),
        _number(nutriments.get("fat_100g")),
    )
    if macros is None:
        return None
    code = product.get("code") or fallback_code
    return FoodItem(
        name=product.get("product_name") or "",
        per100g=macros,
        source=FoodSource.OPEN_FOOD_FACTS,
        external_id=str(code) if code is not None else None,
        confidence=Confidence.MEDIUM,
    )


def _kcal(nutriments: dict, product: dict) -> float | None:
    kcal = _number(nutriments.get("energy-kcal_100g"))
    if kcal is not None:
        return kcal
    kj = _number(nutriments.get("energy-kj_100g"))
    if kj is None and _energy_unit(nutriments, product) == "kj":
        kj = _number(nutriments.get("energy_100g"))
    if kj is None:
        return None
    return kj / KJ_PER_KCAL


def _energy_unit(nutriments: dict, product: dict) -> str:
    unit = nutriments.get("energy_unit") or product.get("energy_unit") or ""
    return str(unit).strip().lower()


def _number(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None
