"""Pantry: thin JARVIS-facing wrapper over the nutrition_core pantry repo."""

import sqlite3

from nutrition_core.models import Macros
from nutrition_core.store import pantry


class PantryStore:
    """Inventory of foods on hand (grams + a per-100g macro snapshot)."""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def add_or_topup(
        self,
        *,
        name: str,
        grams: float,
        per100g: Macros,
        source: str,
        external_id: str | None,
        confidence: str,
    ) -> int:
        """Add stock; same (source, external_id) tops up. Returns the pantry id."""
        return pantry.add_or_topup(
            self._conn,
            name=name,
            grams=grams,
            per100g=per100g,
            source=source,
            external_id=external_id,
            confidence=confidence,
        )

    def list_items(self) -> list[dict]:
        return pantry.list_items(self._conn)

    def set_quantity(self, pantry_id: int, grams: float) -> None:
        pantry.set_quantity(self._conn, pantry_id, grams)

    def adjust_quantity(self, pantry_id: int, delta: float) -> float:
        return pantry.adjust_quantity(self._conn, pantry_id, delta)

    def find_by_food(self, source: str, external_id: str) -> dict | None:
        return pantry.find_by_food(self._conn, source=source, external_id=external_id)

    def remove(self, pantry_id: int) -> None:
        pantry.remove(self._conn, pantry_id)
