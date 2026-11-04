from jarvisd.db import connect
from jarvisd.pantry_store import PantryStore
from jarvisd.tools import suggest
from nutrition_core.models import Confidence, FoodItem, FoodSource, Macros
from nutrition_core.store.goals import set_goals

CHICKEN = Macros(165, 31, 0, 3.6)
RICE = Macros(130, 2.7, 28, 0.3)


def _item(name, macros, ext):
    return FoodItem(name=name, per100g=macros, source=FoodSource.USDA,
                    external_id=ext, confidence=Confidence.HIGH)


class FakeFoods:
    def __init__(self, names=None):
        self.names = names or {}

    def by_barcode(self, code):
        return None

    def lookup(self, query, *, limit=5):
        return self.names.get(query, [])


class Base:
    def setup_method(self):
        self.conn = connect(":memory:")
        self.pantry = PantryStore(self.conn)
        self.chicken_id = self.pantry.add_or_topup(
            name="Chicken", grams=500, per100g=CHICKEN, source="usda",
            external_id="c1", confidence="high")
        self.rice_id = self.pantry.add_or_topup(
            name="Rice", grams=100, per100g=RICE, source="usda",
            external_id="r1", confidence="high")
        self.foods = FakeFoods({"rice": [_item("Rice", RICE, "r1")]})

    def target(self, kcal=2000, p=150, c=200, f=60):
        set_goals(self.conn, Macros(kcal, p, c, f))

    def tool(self, ti):
        h = suggest.handlers(self.conn, self.foods, self.pantry)
        return h["check_dish"](ti)

    def validate(self, ings, name="Bowl"):
        return suggest.validate_dish(name, ings, conn=self.conn,
                                     food_service=self.foods, pantry_store=self.pantry)

    def grams(self):
        return {r["id"]: r["grams_available"] for r in self.pantry.list_items()}


class TestValidate(Base):
    def test_fits(self):
        self.target()
        ings = [{"pantry_id": self.chicken_id, "grams": 200},
                {"pantry_id": self.rice_id, "grams": 100}]
        d = self.validate(ings)
        assert d.ok and d.fit == "ok" and d.pantry_ok and d.shortfalls == ()
        assert round(d.macros.kcal) == 460
        out = self.tool({"name": "Bowl", "ingredients": ings})
        assert "fits" in out and "ready to cook" in out and "NOT logged" in out

    def test_over(self):
        self.target(kcal=300, p=150, c=200, f=60)
        d = self.validate([{"pantry_id": self.chicken_id, "grams": 300}])
        assert d.fit == "over" and d.fit_messages
        out = self.tool({"name": "Big", "ingredients": [{"pantry_id": self.chicken_id, "grams": 300}]})
        assert "over" in out.lower() and "over budget by" in out

    def test_unresolvable_gives_no_macros(self):
        self.target()
        ings = [{"pantry_id": self.chicken_id, "grams": 200}, {"name": "unobtainium", "grams": 50}]
        d = self.validate(ings)
        assert not d.ok and d.macros is None and "unobtainium" in d.reason
        out = self.tool({"name": "X", "ingredients": ings})
        assert "unobtainium" in out and "kcal" not in out and "NOT logged" in out

    def test_pantry_shortfall(self):
        self.target()
        d = self.validate([{"pantry_id": self.rice_id, "grams": 250}])
        assert d.ok and not d.pantry_ok
        assert any("250" in s and "Rice" in s and "100" in s for s in d.shortfalls)
        out = self.tool({"name": "R", "ingredients": [{"pantry_id": self.rice_id, "grams": 250}]})
        assert "short" in out.lower()

    def test_duplicate_rows_aggregated(self):
        self.target()
        ings = [{"pantry_id": self.rice_id, "grams": 60}, {"pantry_id": self.rice_id, "grams": 60}]
        d = self.validate(ings)
        assert not d.pantry_ok and len(d.shortfalls) == 1 and "120" in d.shortfalls[0]
        d = self.validate([{"pantry_id": self.rice_id, "grams": 60}, {"name": "rice", "grams": 60}])
        assert not d.pantry_ok and "120" in d.shortfalls[0]
        assert self.validate([{"pantry_id": self.rice_id, "grams": 100}]).pantry_ok

    def test_shortfall_via_name_lookup(self):
        self.target()
        d = self.validate([{"name": "rice", "grams": 250}])
        assert d.ok and not d.pantry_ok

    def test_non_pantry_ingredient_not_ready(self):
        # Resolves from the food DB but is NOT tracked in the pantry → strictly
        # not ready to cook (you don't have it), even though macros compute fine.
        self.target()
        foods = FakeFoods({"egg": [_item("Egg", Macros(143, 13, 1.1, 9.5), "e1")]})
        ings = [{"name": "egg", "grams": 100}]
        d = suggest.validate_dish("Omelette", ings, conn=self.conn,
                                  food_service=foods, pantry_store=self.pantry)
        assert d.ok and d.macros is not None
        assert not d.pantry_ok
        assert any("not in your pantry" in s.lower() for s in d.shortfalls)
        out = suggest.handlers(self.conn, foods, self.pantry)["check_dish"](
            {"name": "Omelette", "ingredients": ings})
        assert "short" in out.lower() and "not in your pantry" in out.lower()

    def test_no_target(self):
        d = self.validate([{"pantry_id": self.chicken_id, "grams": 100}])
        assert d.ok and d.fit == "no-target" and d.macros is not None
        out = self.tool({"name": "C", "ingredients": [{"pantry_id": self.chicken_id, "grams": 100}]})
        assert "no daily target" in out.lower()

    def test_read_only(self):
        self.target()
        before = self.grams()
        self.tool({"name": "Bowl", "ingredients": [
            {"pantry_id": self.chicken_id, "grams": 200}, {"name": "rice", "grams": 50}]})
        self.validate([{"pantry_id": self.rice_id, "grams": 50}])
        assert self.conn.execute("SELECT COUNT(*) FROM meals").fetchone()[0] == 0
        assert self.conn.execute("SELECT COUNT(*) FROM meal_ingredients").fetchone()[0] == 0
        assert self.grams() == before


class TestBadInput(Base):
    def test_bad(self):
        for ti in (None, {}, {"ingredients": [{"grams": 1}]}, {"name": "x"},
                   {"name": "x", "ingredients": []}, {"name": "x", "ingredients": "no"}):
            out = self.tool(ti)
            assert isinstance(out, str) and out.startswith("Error")
