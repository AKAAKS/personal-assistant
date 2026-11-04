from datetime import date

from jarvisd.db import connect
from jarvisd.pantry_store import PantryStore
from jarvisd.tools import meal_log
from nutrition_core.models import Confidence, FoodItem, FoodSource, Macros
from nutrition_core.store.meals import day_status, day_totals

RICE = Macros(130, 2.7, 28, 0.3)


def _item(name="Rice", macros=RICE, ext="r1"):
    return FoodItem(name=name, per100g=macros, source=FoodSource.USDA,
                    external_id=ext, confidence=Confidence.HIGH)


class FakeFoods:
    def __init__(self, names=None):
        self.names = names or {}

    def by_barcode(self, code):
        return None

    def lookup(self, query, *, limit=5):
        return self.names.get(query, [])


class TestLogEstimated:
    def setup_method(self):
        self.conn = connect(":memory:")
        self.pantry = PantryStore(self.conn)
        self.foods = FakeFoods({"rice": [_item()]})
        self.h = meal_log.handlers(self.conn, self.foods, self.pantry)["log_estimated_meal"]

    def _today(self):
        return date.today().isoformat()

    def test_logs_estimated_with_db_totals_and_confidence(self):
        out = self.h({"description": "Restaurant rice",
                      "ingredients": [{"name": "rice", "grams": 200}], "confidence": "medium"})
        assert not out.startswith("Error")
        assert "estimate" in out.lower()
        row = self.conn.execute("SELECT status, confidence, name FROM meals").fetchone()
        assert row["status"] == "estimated" and row["confidence"] == "medium"
        assert row["name"] == "Restaurant rice"
        t = day_totals(self.conn, self._today())
        assert t.kcal == 260
        assert day_status(self.conn, self._today())["estimated"].kcal == 260

    def test_default_and_invalid_confidence_is_low(self):
        for ti_conf in ({}, {"confidence": "high"}):
            self.h({"description": "x", "ingredients": [{"name": "rice", "grams": 100}], **ti_conf})
        confs = [r["confidence"] for r in self.conn.execute("SELECT confidence FROM meals")]
        assert confs == ["low", "low"]

    def test_never_depletes_pantry(self):
        pid = self.pantry.add_or_topup(name="Rice", grams=1000, per100g=RICE,
                                       source="usda", external_id="r1", confidence="high")
        self.h({"description": "x", "ingredients": [{"pantry_id": pid, "grams": 300},
                                                    {"name": "rice", "grams": 100}]})
        assert self.pantry.list_items()[0]["grams_available"] == 1000
        assert self.conn.execute("SELECT COUNT(*) FROM meals").fetchone()[0] == 1

    def test_one_bad_ingredient_logs_nothing(self):
        out = self.h({"description": "x", "ingredients": [
            {"name": "rice", "grams": 100}, {"name": "unobtainium", "grams": 50}]})
        assert out.startswith(("Error", "Sorry"))
        assert self.conn.execute("SELECT COUNT(*) FROM meals").fetchone()[0] == 0

    def test_never_raises_on_bad_input(self):
        for bad in (None, "x", {}, {"description": "x"}, {"description": "x", "ingredients": []},
                    {"description": "", "ingredients": [{"name": "rice", "grams": 1}]},
                    {"description": "x", "ingredients": [5]}):
            assert isinstance(self.h(bad), str)
        assert self.conn.execute("SELECT COUNT(*) FROM meals").fetchone()[0] == 0
