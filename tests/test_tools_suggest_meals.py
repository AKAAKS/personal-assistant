from tests.test_tools_check_dish import Base
from jarvisd.tools import suggest

NOTHING = "Nothing in your pantry fits what's left today"


class Suggest(Base):
    def run_tool(self, ti):
        return suggest.handlers(self.conn, self.foods, self.pantry)["suggest_meals"](ti)

    def dish(self, name, slot, grams, item="chicken"):
        pid = self.chicken_id if item == "chicken" else self.rice_id
        return {"name": name, "slot": slot, "ingredients": [{"pantry_id": pid, "grams": grams}]}


class TestSuggestMeals(Suggest):
    def test_one_per_slot_all_returned(self):
        self.target()
        out = self.run_tool({"dishes": [
            self.dish("Rice pudding", "sweet", 50, "rice"),
            self.dish("Grilled chicken", "savory", 150),
            self.dish("Chicken scramble", "breakfast", 100)]})
        for s in ("sweet", "savory", "breakfast", "Rice pudding", "Grilled chicken",
                  "Chicken scramble", "kcal", "protein"):
            assert s in out
        assert out.index("sweet") != out.index("savory")
        assert NOTHING not in out

    def test_same_slot_picks_higher_protein(self):
        self.target()
        out = self.run_tool({"dishes": [self.dish("Small", "savory", 100),
                                        self.dish("Big", "savory", 250)]})
        assert "Big" in out and "Small" not in out

    def test_over_budget_excluded(self):
        self.target()
        out = self.run_tool({"dishes": [self.dish("Huge", "savory", 500),
                                        self.dish("Light", "sweet", 100)]})
        assert "Huge" not in out and "Light" in out

    def test_pantry_short_excluded(self):
        self.target()
        out = self.run_tool({"dishes": [self.dish("Rice feast", "sweet", 250, "rice"),
                                        self.dish("Chicken", "savory", 100)]})
        assert "Rice feast" not in out and "Chicken" in out

    def test_unresolvable_excluded_others_shown(self):
        self.target()
        bad = {"name": "Mystery", "slot": "sweet",
               "ingredients": [{"name": "unobtainium", "grams": 50}]}
        out = self.run_tool({"dishes": [bad, self.dish("Chicken", "savory", 100)]})
        assert "Mystery" not in out and "Chicken" in out

    def test_nothing_fits(self):
        self.target(kcal=100, p=150, c=200, f=60)
        out = self.run_tool({"dishes": [self.dish("Huge", "savory", 400),
                                        self.dish("Rice feast", "sweet", 250, "rice")]})
        assert NOTHING in out and "create your own" in out
        assert "Huge" not in out

    def test_read_only(self):
        self.target()
        before = self.grams()
        self.run_tool({"dishes": [self.dish("A", "savory", 150),
                                  self.dish("B", "sweet", 50, "rice")]})
        assert self.conn.execute("SELECT COUNT(*) FROM meals").fetchone()[0] == 0
        assert self.conn.execute("SELECT COUNT(*) FROM meal_ingredients").fetchone()[0] == 0
        assert self.grams() == before

    def test_fit_score(self):
        self.target()
        a = self.validate([{"pantry_id": self.chicken_id, "grams": 100}])
        b = self.validate([{"pantry_id": self.chicken_id, "grams": 200}])
        assert suggest._fit_score(b) > suggest._fit_score(a)
        bad = self.validate([{"name": "unobtainium", "grams": 5}])
        assert suggest._fit_score(bad) == float("-inf")


class TestBadInput(Suggest):
    def test_bad(self):
        ok = self.dish("A", "savory", 100)
        cases = (None, {}, {"dishes": []}, {"dishes": "no"},
                 {"dishes": [ok, ok, ok, ok]},
                 {"dishes": [{**ok, "slot": "dessert"}]},
                 {"dishes": [{"name": "A", "slot": "sweet"}]},
                 {"dishes": [{"name": "A", "slot": "sweet", "ingredients": []}]},
                 {"dishes": ["x"]},
                 {"dishes": [{"slot": "sweet", "ingredients": ok["ingredients"]}]})
        for ti in cases:
            out = self.run_tool(ti)
            assert isinstance(out, str) and out.startswith("Error"), ti
