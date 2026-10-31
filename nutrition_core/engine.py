from __future__ import annotations

from collections.abc import Sequence

from nutrition_core.models import ComputedIngredient, FoodSource, Macros, MealComputation


def compute_ingredient(
    name: str, grams: float, per100g: Macros, source: FoodSource
) -> ComputedIngredient:
    if grams < 0:
        raise ValueError(f"grams must be non-negative, got {grams}")
    # Multiply before dividing so 100 g-based values stay exact in float
    # (e.g. 165 * 140 / 100 == 231.0, whereas 165 * (140 / 100) != 231.0).
    macros = Macros(
        per100g.kcal * grams / 100,
        per100g.protein_g * grams / 100,
        per100g.carbs_g * grams / 100,
        per100g.fat_g * grams / 100,
    )
    return ComputedIngredient(name=name, grams=grams, macros=macros, source=source)


def compute_meal(ingredients: Sequence[ComputedIngredient]) -> MealComputation:
    total = Macros(0, 0, 0, 0)
    for ing in ingredients:
        total = total + ing.macros
    return MealComputation(ingredients=tuple(ingredients), total=total)


def round_portion(grams: float, *, step_g: float = 5.0, unit_g: float | None = None) -> float:
    if grams < 0:
        raise ValueError(f"grams must be non-negative, got {grams}")
    step = unit_g if unit_g is not None else step_g
    if step <= 0:
        raise ValueError(f"rounding step must be positive, got {step}")
    rounded = float(round(grams / step) * step)
    if grams > 0 and rounded == 0:
        rounded = float(step)
    return rounded
