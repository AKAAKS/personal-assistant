"""FoodService: query USDA + Open Food Facts, cache every hit."""

from __future__ import annotations

import logging

from nutrition_core.models import FoodItem
from nutrition_core.store.connection import DbConnection
from nutrition_core.store.food_cache import cache_food

log = logging.getLogger(__name__)


class FoodLookupError(Exception):
    """Every configured food adapter failed."""


class FoodService:
    def __init__(self, conn: DbConnection, *, usda=None, off=None):
        self._conn = conn
        self._adapters = [("usda", usda), ("open_food_facts", off)]

    def lookup(self, query: str, *, limit: int = 5) -> list[FoodItem]:
        """Query USDA then OFF. USDA results come first and can fill the truncated
        list ahead of OFF (OFF items are still cached)."""
        configured = [(n, a) for n, a in self._adapters if a is not None]
        results: list[FoodItem] = []
        failures = 0
        for name, adapter in configured:
            try:
                found = adapter.search(query, limit=limit)
            except Exception as exc:  # network/HTTP: skip this adapter
                failures += 1
                log.warning("food adapter %s failed: %s", name, exc)
                continue
            results.extend(found)
        if configured and failures == len(configured):
            raise FoodLookupError("all food adapters failed")
        for item in results:
            try:
                cache_food(self._conn, item)
            except Exception as exc:  # cache is best-effort; never fail a good lookup
                log.warning("food cache write failed for %s: %s", item.external_id, exc)
        return results[:limit]
