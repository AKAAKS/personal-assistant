from __future__ import annotations

from typing import Protocol

from nutrition_core.models import FoodItem, Macros


class Transport(Protocol):
    def get_json(self, url: str, params: dict | None = None) -> dict: ...


class FoodAdapter(Protocol):
    def search(self, query: str, *, limit: int = 5) -> list[FoodItem]: ...


# Adapters pass this for a macro field that is PRESENT in the source payload but
# unreadable (non-numeric / null / non-finite). An ABSENT field is passed as None
# instead and keeps its 0.0 default.
UNTRUSTWORTHY = float("nan")

MAX_GRAMS_PER_100G = 100.0
MAX_KCAL_PER_100G = 1000.0


def _sane(value: float, upper: float) -> bool:
    return 0.0 <= value <= upper  # NaN fails both comparisons


def macros_from_fields(
    kcal: float | None,
    protein_g: float | None,
    carbs_g: float | None,
    fat_g: float | None,
) -> Macros | None:
    """Build Macros from raw per-100g fields.

    Energy is required; a missing energy value returns None so callers can drop
    the food. ABSENT macro components (None) default to 0.0. A PRESENT-but-
    untrustworthy value (UNTRUSTWORTHY/NaN, negative, protein/carbs/fat > 100 g,
    or kcal > 1000 per 100 g) returns None so the food is dropped.
    """
    if kcal is None:
        return None
    if not _sane(kcal, MAX_KCAL_PER_100G):
        return None
    grams = [0.0 if g is None else g for g in (protein_g, carbs_g, fat_g)]
    if not all(_sane(g, MAX_GRAMS_PER_100G) for g in grams):
        return None
    return Macros(kcal, *grams)
