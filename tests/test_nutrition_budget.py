from nutrition_core.models import Macros
from nutrition_core.budget import assess_day, remaining


class TestRemaining:
    def test_remaining_subtracts(self):
        assert remaining(Macros(2000, 150, 200, 60), Macros(500, 40, 50, 15)) == Macros(1500, 110, 150, 45)

class TestAssessDay:
    def test_ok_when_everything_fits(self):
        assert assess_day(Macros(800, 50, 80, 25)).status == "ok"

    def test_over_when_a_macro_is_exceeded(self):
        a = assess_day(Macros(-120, 10, 10, 5))  # ate more kcal than budget
        assert a.status == "over"

    def test_cornered_when_calories_cannot_hold_remaining_macros(self):
        # 4*120 + 4*0 + 9*0 = 480 kcal minimum, only 300 left -> impossible
        assert assess_day(Macros(300, 120, 0, 0)).status == "cornered"

    def test_cornered_boundary_exact_is_ok(self):
        # floor = 4*120 = 480; 430 + 50 == 480, not < 480 -> ok
        assert assess_day(Macros(430, 120, 0, 0)).status == "ok"

    def test_cornered_just_below_boundary(self):
        # 429 + 50 == 479 < 480 -> cornered
        assert assess_day(Macros(429, 120, 0, 0)).status == "cornered"

    def test_exactly_minus_tolerance_is_not_over(self):
        # kcal check is strict: -50 is not < -50
        assert assess_day(Macros(-50, 10, 10, 5)).status != "over"

    def test_just_past_minus_tolerance_is_over(self):
        assert assess_day(Macros(-51, 10, 10, 5)).status == "over"
