from jarvisd.db import connect
from nutrition_core.models import Confidence, FoodItem, FoodSource, Macros
from nutrition_core.store.food_cache import cache_food, get_cached


def _item(source=FoodSource.USDA, ext="123", macros=Macros(165, 31, 0, 3.6)):
    return FoodItem("Chicken breast", macros, source, ext, Confidence.HIGH)


class TestFoodCache:
    def test_cache_and_get_roundtrip(self):
        conn = connect(":memory:")
        item = _item()
        assert cache_food(conn, item) is True
        got = get_cached(conn, "usda", "123")
        assert got == item
        assert got.confidence == Confidence.HIGH
        assert got.source == FoodSource.USDA

    def test_get_missing_returns_none(self):
        conn = connect(":memory:")
        assert get_cached(conn, "usda", "nope") is None

    def test_upsert_replaces_same_key(self):
        conn = connect(":memory:")
        cache_food(conn, _item())
        cache_food(conn, _item(macros=Macros(200, 25, 1, 8)))
        n = conn.execute("SELECT COUNT(*) AS n FROM food_cache").fetchone()["n"]
        assert n == 1
        assert get_cached(conn, "usda", "123").per100g == Macros(200, 25, 1, 8)

    def test_item_without_external_id_not_cached(self):
        conn = connect(":memory:")
        assert cache_food(conn, _item(ext=None)) is False
        n = conn.execute("SELECT COUNT(*) AS n FROM food_cache").fetchone()["n"]
        assert n == 0

    def test_off_confidence_is_medium(self):
        conn = connect(":memory:")
        cache_food(conn, _item(source=FoodSource.OPEN_FOOD_FACTS, ext="999"))
        got = get_cached(conn, "open_food_facts", "999")
        assert got.confidence == Confidence.MEDIUM
