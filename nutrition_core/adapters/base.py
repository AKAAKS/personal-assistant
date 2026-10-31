from __future__ import annotations

from typing import Protocol

from nutrition_core.models import FoodItem, Macros


class Transport(Protocol):
    def get_json(self, url: str, params: dict | None = None) -> dict: ...


class FoodAdapter(Protocol):
    def search(self, query: str, *, limit: int = 5) -> list[FoodItem]: ...


def macros_from_fields(
    kcal: float | None,
    protein_g: float | None,
    carbs_g: float | None,
    fat_g: float | None,
) -> Macros | None:
    """Build Macros from raw per-100g fields.

    Energy is required; a missing energy value returns None so callers can drop
    the food. Missing macro components default to 0.0.
    """
    if kcal is None:
        return None
    return Macros(
        kcal,
        protein_g if protein_g is not None else 0.0,
        carbs_g if carbs_g is not None else 0.0,
        fat_g if fat_g is not None else 0.0,
    )
