import pytest

from nutrition_core.engine import compute_ingredient, compute_meal, round_portion
from nutrition_core.models import ComputedIngredient, FoodSource, Macros, MealComputation


class TestComputeIngredient:
    def test_scales_per_100g_by_grams(self):
        ci = compute_ingredient("chicken breast", 140, Macros(165, 31, 0, 3.6), FoodSource.USDA)
        assert ci.macros == Macros(231.0, 43.4, 0.0, 5.04)

    def test_zero_grams_is_all_zero(self):
        ci = compute_ingredient("rice", 0, Macros(130, 2.7, 28, 0.3), FoodSource.USDA)
        assert ci.macros == Macros(0, 0, 0, 0)

    def test_negative_grams_raises(self):
        with pytest.raises(ValueError):
            compute_ingredient("rice", -5, Macros(130, 2.7, 28, 0.3), FoodSource.USDA)

class TestComputeMeal:
    def test_totals_are_elementwise_sum(self):
        ings = [
            compute_ingredient("chicken", 100, Macros(165, 31, 0, 3.6), FoodSource.USDA),
            compute_ingredient("rice", 100, Macros(130, 2.7, 28, 0.3), FoodSource.USDA),
        ]
        assert compute_meal(ings).total == Macros(295, 33.7, 28, 3.9)

    def test_empty_meal_is_zero(self):
        assert compute_meal([]).total == Macros(0, 0, 0, 0)

class TestRoundPortion:
    def test_rounds_to_nearest_step(self):
        assert round_portion(137) == 135.0
        assert round_portion(138) == 140.0

    def test_unit_rounding_whole_items(self):
        assert round_portion(130, unit_g=50) == 150.0  # ~2.6 eggs -> 3 units

    def test_positive_input_never_rounds_to_zero(self):
        assert round_portion(2) == 5.0

    def test_rounding_delta_is_observable(self):
        per100 = Macros(155, 13, 1.1, 11)
        requested = compute_ingredient("egg", 132, per100, FoodSource.USDA)
        rounded = compute_ingredient("egg", round_portion(132, unit_g=50), per100, FoodSource.USDA)
        assert rounded.macros != requested.macros  # delta exists and is recomputable
