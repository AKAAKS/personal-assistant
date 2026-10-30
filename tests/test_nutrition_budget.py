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

    def test_boundary_ok_just_above_min_kcal(self):
        # min = 480; 480 + tolerance margin -> ok
        assert assess_day(Macros(520, 120, 0, 0)).status == "ok"
