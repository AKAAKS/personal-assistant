"""Dish suggestion tools: check_dish (deterministic, READ-ONLY dish preview).

The LLM never supplies macros. Every ingredient is resolved to a real snapshot
(reusing meal_log.resolve_ingredient), the engine computes the total, and the
pantry/day budget are only READ. Nothing is logged and nothing is depleted.
"""

from dataclasses import dataclass
from datetime import date

from jarvisd.tools.meal_log import resolve_ingredient
from nutrition_core.budget import assess_day
from nutrition_core.engine import compute_ingredient, compute_meal
from nutrition_core.models import Macros
from nutrition_core.store.meals import day_status as day_status_repo

TOOLS = [
    {
        "name": "check_dish",
        "description": (
            "Preview a proposed dish WITHOUT logging it: computes real macros from food "
            "data, checks the pantry has enough of each ingredient, and says whether it "
            "fits today's remaining budget. Give 'name' and 'ingredients': each has "
            "'grams' plus exactly ONE of 'barcode', 'name' or 'pantry_id'. Read-only: "
            "never logs the meal or changes pantry stock."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Dish name."},
                "ingredients": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "properties": {
                            "grams": {"type": "number", "description": "Grams used (> 0)."},
                            "barcode": {"type": "string"},
                            "name": {"type": "string"},
                            "pantry_id": {"type": "integer"},
                        },
                        "required": ["grams"],
                    },
                },
            },
            "required": ["name", "ingredients"],
        },
    },
]


@dataclass(frozen=True)
class DishCheck:
    name: str
    ok: bool
    reason: str | None
    macros: Macros | None
    fit: str  # 'ok' | 'over' | 'cornered' | 'no-target'
    fit_messages: tuple[str, ...]
    pantry_ok: bool
    shortfalls: tuple[str, ...]


def _g(x: float) -> str:
    return f"{x:g}"


def validate_dish(name, ingredients, *, conn, food_service, pantry_store) -> DishCheck:
    """Resolve, compute, and assess a dish. READ-ONLY: no writes, no depletion."""
    resolved = []
    for spec in ingredients:
        r = resolve_ingredient(spec, food_service=food_service, pantry_store=pantry_store)
        if isinstance(r, str):
            return DishCheck(name, False, r, None, "no-target", (), False, ())
        resolved.append(r)

    total = compute_meal([
        compute_ingredient(r.meal_ingredient.name, r.meal_ingredient.grams,
                           r.meal_ingredient.per100g, r.meal_ingredient.source)
        for r in resolved]).total

    # Pantry availability (reads only).
    items = pantry_store.list_items()
    by_id = {row["id"]: row for row in items}
    need_by_row: dict[int, float] = {}
    rows: dict[int, dict] = {}
    for r in resolved:
        if r.pantry_id is not None:
            row = by_id.get(r.pantry_id)
        elif r.external_id is not None:
            row = pantry_store.find_by_food(r.source, r.external_id)
        else:
            row = None
        if row is not None:
            rows[row["id"]] = row
            need_by_row[row["id"]] = need_by_row.get(row["id"], 0.0) + r.meal_ingredient.grams
    shortfalls = []
    for rid, need in need_by_row.items():
        row = rows[rid]
        if row["grams_available"] < need:
            shortfalls.append(f"needs {_g(need)} g of {row['name']}, "
                              f"have {_g(row['grams_available'])} g")

    rem = day_status_repo(conn, date.today().isoformat())["remaining"]
    if rem is None:
        fit, msgs = "no-target", ()
    else:
        assess = assess_day(rem - total)
        fit, msgs = assess.status, tuple(assess.messages)

    shown = Macros(round(total.kcal, 1), round(total.protein_g, 1),
                   round(total.carbs_g, 1), round(total.fat_g, 1))
    return DishCheck(name, True, None, shown, fit, msgs, not shortfalls, tuple(shortfalls))


def handlers(conn, food_service, pantry_store):
    def check_dish(ti):
        try:
            if not isinstance(ti, dict):
                return "Error: invalid input."
            name = ti.get("name")
            if not isinstance(name, str) or not name.strip():
                return "Error: 'name' (the dish name) is required."
            ings = ti.get("ingredients")
            if not isinstance(ings, list) or not ings:
                return "Error: 'ingredients' must be a non-empty list."
            name = name.strip()
            d = validate_dish(name, ings, conn=conn, food_service=food_service,
                              pantry_store=pantry_store)
            if not d.ok:
                return (f"Couldn't validate '{name}': {d.reason} "
                        "The dish was NOT logged (preview only).")
            m = d.macros
            lines = [f"Preview of '{name}': {m.kcal:.0f} kcal, {m.protein_g:.1f}g protein, "
                     f"{m.carbs_g:.1f}g carbs, {m.fat_g:.1f}g fat."]
            if d.fit == "no-target":
                lines.append("Fit: no daily target set.")
            elif d.fit == "ok":
                rem = day_status_repo(conn, date.today().isoformat())["remaining"]
                head = rem.kcal - d.macros.kcal
                lines.append(f"Fit: fits — {head:.0f} kcal headroom.")
            elif d.fit == "over":
                lines.append("Fit: over — " + "; ".join(d.fit_messages) + ".")
            else:
                lines.append("Fit: corners you — " + "; ".join(d.fit_messages) + ".")
            if d.pantry_ok:
                lines.append("Pantry: ready to cook with what you have.")
            else:
                lines.append("Pantry: short — " + "; ".join(d.shortfalls) + ".")
            lines.append("This dish was NOT logged (preview only); pantry unchanged.")
            return "\n".join(lines)
        except Exception as exc:
            return f"Error: couldn't check the dish ({exc})"

    return {"check_dish": check_dish}
