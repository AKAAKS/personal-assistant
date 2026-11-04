from datetime import date, timedelta

from jarvisd.tools import prep
from nutrition_core.store.meals import day_totals, list_planned
from tests.test_tools_plan_prep import PrepBase

TOMORROW = (date.today() + timedelta(days=1)).isoformat()


class CookBase(PrepBase):
    def h(self):
        return prep.handlers(self.conn, self.foods, self.pantry)

    def cook(self, mid):
        return self.h()["cook_meal"]({"meal_id": mid})

    def lock(self, name, ings, day=None):
        self.run_tool({"dishes": [self.dish(name, ings, day or TOMORROW)]})
        return list_planned(self.conn)[-1]["id"]


def test_exports():
    assert [t["name"] for t in prep.TOOLS] == ["plan_prep", "cook_meal", "list_planned_meals"]


class TestCook(CookBase):
    def test_cook_flips_and_depletes_pantry_id_link(self):
        mid = self.lock("Bowl", [self.chicken(200), self.rice(50)], date.today().isoformat())
        assert day_totals(self.conn, date.today().isoformat()).kcal == 0
        out = self.cook(mid)
        assert day_totals(self.conn, date.today().isoformat()).kcal > 0
        g = self.grams()
        assert g[self.chicken_id] == 300 and g[self.rice_id] == 50
        assert "Bowl" in out and "verified" in out.lower()

    def test_cook_depletes_source_external_id_link(self):
        mid = self.lock("Eggs", [{"name": "rice", "grams": 40}])  # resolved via food_service
        out = self.cook(mid)
        assert self.grams()[self.rice_id] == 60
        assert "Rice" in out or "rice" in out

    def test_future_dated_cook_reports_planned_date(self):
        mid = self.lock("Ahead", [self.chicken(200)])  # TOMORROW
        out = self.cook(mid)
        assert TOMORROW in out and "today" not in out.lower()
        assert self.grams()[self.chicken_id] == 300
        assert day_totals(self.conn, date.today().isoformat()).kcal == 0
        assert day_totals(self.conn, TOMORROW).kcal > 0

    def test_second_cook_is_safe_no_double_deplete(self):
        mid = self.lock("Bowl", [self.chicken(200)])
        self.cook(mid)
        after_first = self.grams()
        out = self.cook(mid)
        assert self.grams() == after_first
        assert "nothing was depleted" in out.lower()

    def test_missing_pantry_row_still_cooks(self):
        mid = self.lock("Bowl", [self.chicken(200), self.rice(50)])
        self.pantry.remove(self.rice_id)
        out = self.cook(mid)
        g = self.grams()
        assert g == {self.chicken_id: 300}
        assert all(v >= 0 for v in g.values())
        assert "couldn't deplete" in out.lower() and "Rice" in out
        assert list_planned(self.conn) == []

    def test_bad_ids(self):
        h = self.h()["cook_meal"]
        assert h({"meal_id": 9999}).lower().startswith(("that meal", "error"))
        assert h({"meal_id": "abc"}).startswith("Error")
        assert h({}).startswith("Error")
        assert h({"meal_id": True}).startswith("Error")


class TestList(CookBase):
    def test_lists_and_omits_cooked(self):
        a = self.lock("Alpha", [self.chicken(100)])
        b = self.lock("Beta", [self.rice(20)])
        lst = self.h()["list_planned_meals"]
        out = lst({})
        assert "Alpha" in out and "Beta" in out and str(a) in out and "kcal" in out
        self.cook(a)
        out = lst({})
        assert "Alpha" not in out and "Beta" in out

    def test_empty_and_bad_date(self):
        lst = self.h()["list_planned_meals"]
        assert "no planned" in lst({}).lower()
        assert lst({"from_date": "nope"}).startswith("Error")
