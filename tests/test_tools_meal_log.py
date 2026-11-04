from datetime import date

from jarvisd.db import connect
from jarvisd.pantry_store import PantryStore
from jarvisd.tools import meal_log
from nutrition_core.food_service import FoodLookupError
from nutrition_core.models import Confidence, FoodItem, FoodSource, Macros
from nutrition_core.store.meals import day_status, day_totals

CHICKEN = Macros(165, 31, 0, 3.6)
RICE = Macros(130, 2.7, 28, 0.3)


def _item(name="Rice", macros=RICE, ext="r1", source=FoodSource.USDA):
    return FoodItem(name=name, per100g=macros, source=source,
                    external_id=ext, confidence=Confidence.HIGH)


class FakeFoods:
    def __init__(self, barcodes=None, names=None):
        self.barcodes = barcodes or {}
        self.names = names or {}

    def by_barcode(self, code):
        return self.barcodes.get(code)

    def lookup(self, query, *, limit=5):
        return self.names.get(query, [])


class RaisingFoods:
    def by_barcode(self, code):
        raise FoodLookupError("down")

    def lookup(self, query, *, limit=5):
        raise FoodLookupError("down")


def _today():
    return date.today().isoformat()


class TestLogMeal:
    def setup_method(self):
        self.conn = connect(":memory:")
        self.pantry = PantryStore(self.conn)
        self.foods = FakeFoods(
            barcodes={"111": _item("Rice", RICE, "r1")},
            names={"rice": [_item("Rice", RICE, "r1")]},
        )

    def _log(self, ti, foods=None):
        h = meal_log.handlers(self.conn, foods if foods is not None else self.foods, self.pantry)
        return h["log_meal"](ti)

    def _meals(self):
        return self.conn.execute("SELECT status FROM meals").fetchall()

    def _ing_count(self):
        return self.conn.execute("SELECT COUNT(*) FROM meal_ingredients").fetchone()[0]

    def test_name_ingredient_logs_verified_exact_totals(self):
        out = self._log({"name": "Rice bowl",
                         "ingredients": [{"name": "rice", "grams": 200}]})
        assert not out.startswith("Error")
        rows = self._meals()
        assert len(rows) == 1 and rows[0]["status"] == "verified"
        t = day_totals(self.conn, _today())
        assert t.kcal == 260 and abs(t.protein_g - 5.4) < 1e-9
        assert day_status(self.conn, _today())["verified"].kcal == 260

    def test_pantry_id_logs_and_depletes(self):
        pid = self.pantry.add_or_topup(name="Chicken", grams=500, per100g=CHICKEN,
                                       source="usda", external_id="c1", confidence="high")
        out = self._log({"name": "Chicken", "ingredients": [{"pantry_id": pid, "grams": 200}]})
        assert not out.startswith("Error")
        assert day_totals(self.conn, _today()).kcal == 330
        item = self.pantry.list_items()[0]
        assert item["grams_available"] == 300

    def test_barcode_matching_pantry_depletes(self):
        self.pantry.add_or_topup(name="Rice", grams=1000, per100g=RICE,
                                 source="usda", external_id="r1", confidence="high")
        out = self._log({"name": "Rice", "ingredients": [{"barcode": "111", "grams": 150}]})
        assert not out.startswith("Error")
        assert self.pantry.list_items()[0]["grams_available"] == 850
        assert len(self._meals()) == 1

    def test_one_failure_aborts_everything(self):
        pid = self.pantry.add_or_topup(name="Chicken", grams=500, per100g=CHICKEN,
                                       source="usda", external_id="c1", confidence="high")
        bads = [
            {"barcode": "nope", "grams": 50},
            {"pantry_id": 9999, "grams": 50},
            {"name": "rice", "grams": 0},
        ]
        for bad in bads:
            out = self._log({"name": "Mix", "ingredients": [
                {"pantry_id": pid, "grams": 100}, bad]})
            assert isinstance(out, str) and out
            assert self._meals() == []
            assert self._ing_count() == 0
            assert day_totals(self.conn, _today()).kcal == 0
            assert self.pantry.list_items()[0]["grams_available"] == 500
        out = self._log({"name": "Mix", "ingredients": [{"barcode": "nope", "grams": 5}]})
        assert "nope" in out
        out = self._log({"name": "Mix", "ingredients": [{"pantry_id": 9999, "grams": 5}]})
        assert "9999" in out

    def test_depletion_raises_still_logged(self, monkeypatch):
        self.pantry.add_or_topup(name="Rice", grams=1000, per100g=RICE,
                                 source="usda", external_id="r1", confidence="high")

        def boom(**kw):
            raise RuntimeError("db locked")
        monkeypatch.setattr(self.pantry, "deplete", boom)
        out = self._log({"name": "Rice", "ingredients": [{"barcode": "111", "grams": 100}]})
        assert len(self._meals()) == 1
        assert not out.startswith("Error")
        assert "pantry" in out.lower() and "warning" in out.lower()

    def test_over_deplete_removes_row(self):
        pid = self.pantry.add_or_topup(name="Chicken", grams=150, per100g=CHICKEN,
                                       source="usda", external_id="c1", confidence="high")
        self._log({"name": "Chicken", "ingredients": [{"pantry_id": pid, "grams": 200}]})
        assert len(self._meals()) == 1
        assert self.pantry.list_items() == []

    def test_lookup_error_is_graceful(self):
        out = self._log({"name": "X", "ingredients": [{"name": "rice", "grams": 5}]},
                        foods=RaisingFoods())
        assert "food database" in out
        assert self._meals() == []

    def test_no_food_service(self):
        h = meal_log.handlers(self.conn, None, self.pantry)
        out = h["log_meal"]({"name": "X", "ingredients": [{"name": "rice", "grams": 5}]})
        assert isinstance(out, str) and self._meals() == []

    def test_bad_input_never_raises(self):
        bads = [
            {},
            {"ingredients": [{"name": "rice", "grams": 5}]},
            {"name": "X"},
            {"name": "X", "ingredients": []},
            {"name": "X", "ingredients": "rice"},
            {"name": "X", "ingredients": ["rice"]},
            {"name": "X", "ingredients": [{"grams": 5}]},
            {"name": "X", "ingredients": [{"name": "rice", "barcode": "111", "grams": 5}]},
            {"name": "X", "ingredients": [{"name": "rice", "grams": True}]},
            {"name": "X", "ingredients": [{"name": "rice", "grams": float("nan")}]},
            {"name": "", "ingredients": [{"name": "rice", "grams": 5}]},
        ]
        for ti in bads:
            out = self._log(ti)
            assert isinstance(out, str) and out
        assert self._meals() == []
        assert meal_log.handlers(self.conn, self.foods, self.pantry)["log_meal"](None)
