from datetime import date, timedelta

from jarvisd.tools import prep
from nutrition_core.models import Macros
from nutrition_core.store.meals import day_status, list_planned
from tests.test_tools_check_dish import Base, _item

TOMORROW = (date.today() + timedelta(days=1)).isoformat()


class PrepBase(Base):
    def setup_method(self):
        super().setup_method()
        self.foods.names["egg"] = [_item("Egg", Macros(143, 13, 1.1, 9.5), "e1")]

    def run_tool(self, ti):
        return prep.handlers(self.conn, self.foods, self.pantry)["plan_prep"](ti)

    def dish(self, name, ings, day=TOMORROW):
        return {"name": name, "date": day, "ingredients": ings}

    def chicken(self, g):
        return {"pantry_id": self.chicken_id, "grams": g}

    def rice(self, g):
        return {"pantry_id": self.rice_id, "grams": g}


def test_exports():
    assert [t["name"] for t in prep.TOOLS] == ["plan_prep"]


class TestLock(PrepBase):
    def test_locks_two_planned_no_budget_no_pantry_change(self):
        self.target()
        before_rem = day_status(self.conn, date.today().isoformat())["remaining"]
        before = self.grams()
        out = self.run_tool({"dishes": [self.dish("Chicken bowl", [self.chicken(200), self.rice(50)]),
                                        self.dish("Plain chicken", [self.chicken(100)])]})
        planned = list_planned(self.conn)
        assert [p["name"] for p in planned] == ["Chicken bowl", "Plain chicken"]
        assert all(p["date"].startswith(TOMORROW) for p in planned)
        assert "Chicken bowl" in out and "Plain chicken" in out and "kcal" in out
        assert day_status(self.conn, date.today().isoformat())["remaining"] == before_rem
        assert self.grams() == before


class TestShoppingDelta(PrepBase):
    def test_four_cases(self):
        # chicken: 300+300=600 vs 500 -> buy 100 (summed once, partial)
        # rice: 50 vs 100 -> fully stocked, omitted
        # egg: absent from pantry -> full 100
        out = self.run_tool({"dishes": [
            self.dish("A", [self.chicken(300), self.rice(50)]),
            self.dish("B", [self.chicken(300), {"name": "egg", "grams": 100}])]})
        assert len(list_planned(self.conn)) == 2
        assert "Chicken" in out and "100" in out
        assert "Egg" in out
        shopping = out.split("Shopping", 1)[1]
        assert "Rice" not in shopping
        assert "Nothing to buy" not in out

    def test_delta_structure(self):
        from jarvisd.tools.meal_log import resolve_ingredient
        res = [[resolve_ingredient(s, food_service=self.foods, pantry_store=self.pantry)
                for s in ings] for ings in (
            [self.chicken(300), self.rice(50)],
            [self.chicken(300), {"name": "egg", "grams": 100}])]
        delta = {d["name"]: d for d in prep._shopping_delta(res, self.pantry)}
        assert set(delta) == {"Chicken", "Egg"} or {d.lower() for d in delta} == {"chicken", "egg"}
        c = next(d for k, d in delta.items() if k.lower() == "chicken")
        assert c["need_g"] == 600 and c["have_g"] == 500 and c["buy_g"] == 100
        e = next(d for k, d in delta.items() if k.lower() == "egg")
        assert e["need_g"] == 100 and e["have_g"] == 0 and e["buy_g"] == 100

    def test_nothing_to_buy(self):
        out = self.run_tool({"dishes": [self.dish("A", [self.chicken(100)])]})
        assert "Nothing to buy" in out


class TestSkip(PrepBase):
    def test_unresolvable_skipped_valid_locked(self):
        out = self.run_tool({"dishes": [
            self.dish("Mystery", [{"name": "unobtainium", "grams": 50}]),
            self.dish("Good", [self.chicken(100)])]})
        names = [p["name"] for p in list_planned(self.conn)]
        assert names == ["Good"]
        assert "Mystery" in out and "unobtainium" in out

    def test_bad_date_skipped(self):
        out = self.run_tool({"dishes": [self.dish("X", [self.chicken(100)], day="tomorrow")]})
        assert list_planned(self.conn) == [] and "X" in out


class TestBadInput(PrepBase):
    def test_errors_never_raise(self):
        for ti in (None, {}, {"dishes": []}, {"dishes": "x"}, {"dishes": [5]},
                   {"dishes": [{"name": "a", "date": TOMORROW}]},
                   {"dishes": [{"date": TOMORROW, "ingredients": [self.chicken(1)]}]},
                   {"dishes": [{"name": "a", "ingredients": [self.chicken(1)]}]}):
            out = self.run_tool(ti)
            assert isinstance(out, str) and out.startswith("Error")
        assert list_planned(self.conn) == []
