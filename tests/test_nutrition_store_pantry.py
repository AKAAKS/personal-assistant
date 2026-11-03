from jarvisd.db import connect
from nutrition_core.models import Macros
from nutrition_core.store import pantry


def _add(conn, grams=200.0, source="usda", external_id="1", macros=None, name="Chicken"):
    return pantry.add_or_topup(
        conn,
        name=name,
        grams=grams,
        per100g=macros or Macros(165, 31, 0, 3.6),
        source=source,
        external_id=external_id,
        confidence="high",
    )


class TestPantryRepo:
    def setup_method(self):
        self.conn = connect(":memory:")

    def test_add_round_trips(self):
        pid = _add(self.conn)
        items = pantry.list_items(self.conn)
        assert len(items) == 1
        it = items[0]
        assert it["id"] == pid
        assert it["name"] == "Chicken"
        assert it["grams_available"] == 200.0
        assert it["source"] == "usda"
        assert it["external_id"] == "1"
        assert it["per100_kcal"] == 165
        assert it["per100_protein_g"] == 31
        assert it["per100_carbs_g"] == 0
        assert it["per100_fat_g"] == 3.6
        assert it["confidence"] == "high"

    def test_topup_same_food_one_row_and_refreshes_snapshot(self):
        a = _add(self.conn, grams=200)
        b = _add(self.conn, grams=150, macros=Macros(170, 30, 1, 4))
        assert a == b
        items = pantry.list_items(self.conn)
        assert len(items) == 1
        assert items[0]["grams_available"] == 350
        assert items[0]["per100_kcal"] == 170
        assert items[0]["per100_fat_g"] == 4

    def test_manual_never_merges(self):
        a = _add(self.conn, source="manual", external_id=None)
        b = _add(self.conn, source="manual", external_id=None)
        assert a != b
        assert len(pantry.list_items(self.conn)) == 2

    def test_adjust_deplete_partial(self):
        pid = _add(self.conn, grams=200)
        assert pantry.adjust_quantity(self.conn, pid, -50) == 150
        assert pantry.list_items(self.conn)[0]["grams_available"] == 150

    def test_adjust_overdraw_floors_and_deletes(self):
        pid = _add(self.conn, grams=150)
        assert pantry.adjust_quantity(self.conn, pid, -999) == 0.0
        assert pantry.list_items(self.conn) == []

    def test_adjust_to_exactly_zero_deletes(self):
        pid = _add(self.conn, grams=100)
        assert pantry.adjust_quantity(self.conn, pid, -100) == 0.0
        assert pantry.list_items(self.conn) == []

    def test_adjust_positive_adds(self):
        pid = _add(self.conn, grams=100)
        assert pantry.adjust_quantity(self.conn, pid, 25) == 125

    def test_set_quantity_absolute(self):
        pid = _add(self.conn, grams=100)
        pantry.set_quantity(self.conn, pid, 42)
        assert pantry.list_items(self.conn)[0]["grams_available"] == 42

    def test_find_by_food(self):
        pid = _add(self.conn)
        hit = pantry.find_by_food(self.conn, source="usda", external_id="1")
        assert hit is not None and hit["id"] == pid
        assert pantry.find_by_food(self.conn, source="usda", external_id="2") is None

    def test_remove(self):
        pid = _add(self.conn)
        pantry.remove(self.conn, pid)
        assert pantry.list_items(self.conn) == []
