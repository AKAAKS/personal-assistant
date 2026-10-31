from nutrition_core.models import FoodSource, Macros, MealIngredient


class TestMacrosArithmetic:
    def test_add_is_elementwise(self):
        a = Macros(100, 10, 20, 5)
        b = Macros(50, 5, 0, 2)
        assert a + b == Macros(150, 15, 20, 7)

    def test_sub_allows_negative(self):
        assert (Macros(100, 10, 20, 5) - Macros(120, 4, 8, 9)) == Macros(-20, 6, 12, -4)

    def test_scaled_multiplies_each_field(self):
        assert Macros(100, 10, 20, 5).scaled(0.5) == Macros(50, 5, 10, 2.5)

    def test_rounded_rounds_each_field(self):
        assert Macros(100.04, 10.06, 20.0, 5.55).rounded(1) == Macros(100.0, 10.1, 20.0, 5.6)


class TestMealIngredient:
    def test_holds_name_grams_per100g_source(self):
        mi = MealIngredient("egg", 100, Macros(155, 13, 1.1, 11), FoodSource.USDA)
        assert mi.grams == 100 and mi.per100g.protein_g == 13 and mi.source is FoodSource.USDA
