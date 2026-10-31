from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum


class FoodSource(str, Enum):
    USDA = "usda"
    OPEN_FOOD_FACTS = "open_food_facts"
    MANUAL = "manual"


class Confidence(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


def _round_half_up(value: float, ndigits: int) -> float:
    # Round from the shortest decimal repr so 5.55 -> 5.6 (not the binary 5.5).
    quantum = Decimal(1).scaleb(-ndigits)
    return float(Decimal(repr(value)).quantize(quantum, rounding=ROUND_HALF_UP))


@dataclass(frozen=True)
class Macros:
    kcal: float
    protein_g: float
    carbs_g: float
    fat_g: float

    def __add__(self, other: Macros) -> Macros:
        return Macros(
            self.kcal + other.kcal,
            self.protein_g + other.protein_g,
            self.carbs_g + other.carbs_g,
            self.fat_g + other.fat_g,
        )

    def __sub__(self, other: Macros) -> Macros:
        return Macros(
            self.kcal - other.kcal,
            self.protein_g - other.protein_g,
            self.carbs_g - other.carbs_g,
            self.fat_g - other.fat_g,
        )

    def scaled(self, factor: float) -> Macros:
        return Macros(
            self.kcal * factor,
            self.protein_g * factor,
            self.carbs_g * factor,
            self.fat_g * factor,
        )

    def rounded(self, ndigits: int = 1) -> Macros:
        return Macros(
            _round_half_up(self.kcal, ndigits),
            _round_half_up(self.protein_g, ndigits),
            _round_half_up(self.carbs_g, ndigits),
            _round_half_up(self.fat_g, ndigits),
        )


@dataclass(frozen=True)
class FoodItem:
    name: str
    per100g: Macros
    source: FoodSource
    external_id: str | None
    confidence: Confidence


@dataclass(frozen=True)
class MealIngredient:
    name: str
    grams: float
    per100g: Macros
    source: FoodSource


@dataclass(frozen=True)
class ComputedIngredient:
    name: str
    grams: float
    macros: Macros
    source: FoodSource


@dataclass(frozen=True)
class MealComputation:
    ingredients: tuple[ComputedIngredient, ...]
    total: Macros
