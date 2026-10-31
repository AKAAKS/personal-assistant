"""Food cache repository: persist resolved foods so lookups aren't repeated."""

from __future__ import annotations

from nutrition_core.models import Confidence, FoodItem, FoodSource, Macros
from nutrition_core.store.connection import DbConnection

# The table has no confidence column; it is derived from the source.
_CONFIDENCE_BY_SOURCE = {
    FoodSource.USDA.value: Confidence.HIGH,
    FoodSource.OPEN_FOOD_FACTS.value: Confidence.MEDIUM,
}


def cache_food(conn: DbConnection, item: FoodItem) -> bool:
    """Upsert a resolved food; return False (nothing cached) if it has no external_id."""
    if item.external_id is None:
        return False
    conn.execute(
        "INSERT INTO food_cache (source, external_id, name, per100_kcal, "
        "per100_protein_g, per100_carbs_g, per100_fat_g, fetched_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now')) "
        "ON CONFLICT(source, external_id) DO UPDATE SET "
        "name=excluded.name, per100_kcal=excluded.per100_kcal, "
        "per100_protein_g=excluded.per100_protein_g, "
        "per100_carbs_g=excluded.per100_carbs_g, "
        "per100_fat_g=excluded.per100_fat_g, fetched_at=datetime('now')",
        (
            item.source.value,
            item.external_id,
            item.name,
            item.per100g.kcal,
            item.per100g.protein_g,
            item.per100g.carbs_g,
            item.per100g.fat_g,
        ),
    )
    conn.commit()
    return True


def get_cached(conn: DbConnection, source: str, external_id: str) -> FoodItem | None:
    row = conn.execute(
        "SELECT name, per100_kcal, per100_protein_g, per100_carbs_g, per100_fat_g "
        "FROM food_cache WHERE source=? AND external_id=?",
        (source, external_id),
    ).fetchone()
    if row is None:
        return None
    return FoodItem(
        row["name"],
        Macros(
            row["per100_kcal"],
            row["per100_protein_g"],
            row["per100_carbs_g"],
            row["per100_fat_g"],
        ),
        FoodSource(source),
        external_id,
        _CONFIDENCE_BY_SOURCE.get(source, Confidence.LOW),
    )
