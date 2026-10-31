from jarvisd.tools import food
from nutrition_core.food_service import FoodLookupError
from nutrition_core.models import Confidence, FoodItem, FoodSource, Macros


class FakeSvc:
    def __init__(self, items=None, exc=None):
        self.items, self.exc = items or [], exc

    def lookup(self, query, *, limit=5):
        if self.exc:
            raise self.exc
        return self.items


def _run(svc, ti):
    return food.handlers(svc)["lookup_food"](ti)


class TestLookupFood:
    def test_tool_def(self):
        assert food.TOOLS[0]["name"] == "lookup_food"
        assert food.TOOLS[0]["input_schema"]["required"] == ["query"]

    def test_lists_candidates(self):
        it = FoodItem("Chicken breast", Macros(165, 31, 0, 3.6), FoodSource.USDA, "171477", Confidence.HIGH)
        out = _run(FakeSvc([it]), {"query": "chicken"})
        assert "Chicken breast" in out
        assert "165 kcal / 31g P / 0g C / 3.6g F per 100g" in out
        assert "usda:171477" in out

    def test_no_results(self):
        out = _run(FakeSvc([]), {"query": "zzz"})
        assert "no matches" in out.lower() and not out.startswith("Error")

    def test_service_error(self):
        out = _run(FakeSvc(exc=FoodLookupError("x")), {"query": "rice"})
        assert "couldn't reach the food database" in out
        assert "Traceback" not in out

    def test_requires_query(self):
        for ti in ({}, {"query": "  "}, {"query": 5}):
            assert _run(FakeSvc(), ti).startswith("Error")

    def test_unexpected_exception_never_raises(self):
        assert _run(FakeSvc(exc=ValueError("bad")), {"query": "x"}).startswith("Error")
