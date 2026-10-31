"""Meals repository: verified meal logging, daily totals, remaining budget.

All numbers are computed by the deterministic engine from per-100g data;
nothing here accepts a caller-supplied total.
"""

from __future__ import annotations

from collections.abc import Sequence

from nutrition_core.budget import remaining
from nutrition_core.engine import compute_ingredient, compute_meal
from nutrition_core.models import Macros, MealIngredient
from nutrition_core.store.connection import DbConnection
from nutrition_core.store.goals import active_goals


def log_verified_meal(
    conn: DbConnection,
    *,
    name: str,
    date: str,
    ingredients: Sequence[MealIngredient],
    status: str = "eaten",
) -> int:
    """Persist a meal whose macros are computed from ingredient grams x per-100g.

    The legacy calories/protein_g/carbs_g/fat_g columns receive the computed
    total (calories rounded half-up to an integer) so MealStore summaries keep
    working. Each ingredient row stores the per-100g snapshot and the computed
    contribution. One transaction, one commit.
    """
    computed = [
        compute_ingredient(i.name, i.grams, i.per100g, i.source) for i in ingredients
    ]
    total = compute_meal(computed).total
    legacy_kcal = int(Macros(total.kcal, 0, 0, 0).rounded(0).kcal)  # half-up
    # Pre-assemble every ingredient row BEFORE the first write so a bad value
    # (e.g. a non-enum source) raises before anything is persisted.
    ing_rows = [
        (
            ing.name, ing.grams,
            ing.per100g.kcal, ing.per100g.protein_g, ing.per100g.carbs_g, ing.per100g.fat_g,
            ing.source.value,
            comp.macros.kcal, comp.macros.protein_g, comp.macros.carbs_g, comp.macros.fat_g,
        )
        for ing, comp in zip(ingredients, computed)
    ]
    cur = conn.execute(
        "INSERT INTO meals (ts, description, quantity, calories, protein_g, carbs_g, "
        "fat_g, confidence, assumptions, status, name) "
        "VALUES (?, ?, '', ?, ?, ?, ?, 'high', '', ?, ?)",
        (date, name, legacy_kcal, total.protein_g, total.carbs_g, total.fat_g, status, name),
    )
    meal_id = cur.lastrowid
    for row in ing_rows:
        conn.execute(
            "INSERT INTO meal_ingredients (meal_id, name, grams, per100_kcal, "
            "per100_protein_g, per100_carbs_g, per100_fat_g, source, kcal, protein_g, "
            "carbs_g, fat_g) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (meal_id, *row),
        )
    conn.commit()
    return meal_id


def day_totals(conn: DbConnection, date: str) -> Macros:
    """Sum of all meals (any status) whose ts starts with `date`.

    Verified meals are summed from their exact per-ingredient contributions
    (the legacy calories column is integer-rounded); meals without ingredient
    rows (legacy estimates) use their legacy columns.
    """
    total = Macros(0, 0, 0, 0)
    meals = conn.execute(
        "SELECT id, calories, protein_g, carbs_g, fat_g FROM meals WHERE ts LIKE ? ORDER BY id",
        (date + "%",),
    ).fetchall()
    for m in meals:
        ings = conn.execute(
            "SELECT kcal, protein_g, carbs_g, fat_g FROM meal_ingredients "
            "WHERE meal_id=? ORDER BY id",
            (m["id"],),
        ).fetchall()
        rows = ings if ings else [m]
        for r in rows:
            total = total + Macros(r["calories" if not ings else "kcal"],
                                   r["protein_g"], r["carbs_g"], r["fat_g"])
    return total


def remaining_today(conn: DbConnection, date: str) -> Macros | None:
    goal = active_goals(conn)
    if goal is None:
        return None
    return remaining(goal, day_totals(conn, date))
