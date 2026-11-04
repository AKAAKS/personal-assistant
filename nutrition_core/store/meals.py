"""Meals repository: verified meal logging, daily totals, remaining budget.

All numbers are computed by the deterministic engine from per-100g data;
nothing here accepts a caller-supplied total.
"""

from __future__ import annotations

from collections.abc import Sequence

from nutrition_core.budget import FitAssessment, assess_day, remaining
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
    confidence: str = "high",
    assumptions: str = "",
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
        "VALUES (?, ?, '', ?, ?, ?, ?, ?, ?, ?, ?)",
        (date, name, legacy_kcal, total.protein_g, total.carbs_g, total.fat_g,
         confidence, assumptions, status, name),
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


def _day_meal_totals(conn: DbConnection, date: str) -> list[tuple[str, Macros]]:
    """(status, exact macros) per meal whose ts starts with `date`.

    A meal's macros are the sum of its exact per-ingredient contributions (the
    legacy calories column is integer-rounded); meals without ingredient rows
    (legacy estimates) use their legacy columns.
    """
    out: list[tuple[str, Macros]] = []
    meals = conn.execute(
        "SELECT id, status, calories, protein_g, carbs_g, fat_g FROM meals "
        "WHERE ts LIKE ? ORDER BY id",
        (date + "%",),
    ).fetchall()
    for m in meals:
        ings = conn.execute(
            "SELECT kcal, protein_g, carbs_g, fat_g FROM meal_ingredients "
            "WHERE meal_id=? ORDER BY id",
            (m["id"],),
        ).fetchall()
        total = Macros(0, 0, 0, 0)
        if ings:
            for r in ings:
                total = total + Macros(r["kcal"], r["protein_g"], r["carbs_g"], r["fat_g"])
        else:
            total = Macros(m["calories"], m["protein_g"], m["carbs_g"], m["fat_g"])
        out.append((m["status"], total))
    return out


def day_totals(conn: DbConnection, date: str) -> Macros:
    """Sum of all meals (any status) whose ts starts with `date`."""
    total = Macros(0, 0, 0, 0)
    for _status, macros in _day_meal_totals(conn, date):
        total = total + macros
    return total


def day_status(conn: DbConnection, date: str) -> dict:
    """Day's intake split into verified vs estimated, against the active target."""
    verified = Macros(0, 0, 0, 0)
    estimated = Macros(0, 0, 0, 0)
    for status, macros in _day_meal_totals(conn, date):
        if status == "estimated":
            estimated = estimated + macros
        else:
            verified = verified + macros
    consumed = verified + estimated
    target = active_goals(conn)
    rem = remaining(target, consumed) if target is not None else None
    assessment: FitAssessment | None = assess_day(rem) if rem is not None else None
    return {
        "target": target,
        "verified": verified,
        "estimated": estimated,
        "consumed": consumed,
        "remaining": rem,
        "assessment": assessment,
    }


def remaining_today(conn: DbConnection, date: str) -> Macros | None:
    goal = active_goals(conn)
    if goal is None:
        return None
    return remaining(goal, day_totals(conn, date))
