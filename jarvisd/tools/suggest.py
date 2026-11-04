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
    {
        "name": "suggest_meals",
        "description": (
            "Propose 1-3 candidate dishes made from the user's pantry (each tagged with a "
            "'slot': sweet, savory or breakfast) and get back the ones that really fit: each "
            "is validated with real macros, kept only if it fits today's remaining budget AND "
            "the pantry has enough, and the best one per slot is returned. Each dish has "
            "'name', 'slot' and 'ingredients' (each 'grams' plus exactly ONE of 'barcode', "
            "'name' or 'pantry_id'). Read-only: never logs a meal or changes pantry stock."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "dishes": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 3,
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "slot": {"type": "string", "enum": ["sweet", "savory", "breakfast"]},
                            "ingredients": {
                                "type": "array",
                                "minItems": 1,
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "grams": {"type": "number"},
                                        "barcode": {"type": "string"},
                                        "name": {"type": "string"},
                                        "pantry_id": {"type": "integer"},
                                    },
                                    "required": ["grams"],
                                },
                            },
                        },
                        "required": ["name", "slot", "ingredients"],
                    },
                },
            },
            "required": ["dishes"],
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

    # Pantry availability (reads only). Suggestions are strictly pantry-constrained:
    # an ingredient that resolves but is NOT tracked in the pantry makes the dish
    # not-ready (you can't cook it with what you have), not silently "available".
    items = pantry_store.list_items()
    by_id = {row["id"]: row for row in items}
    need_by_row: dict[int, float] = {}
    rows: dict[int, dict] = {}
    not_in_pantry: list[str] = []
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
        else:
            label = r.meal_ingredient.name
            if label not in not_in_pantry:
                not_in_pantry.append(label)
    shortfalls = []
    for rid, need in need_by_row.items():
        row = rows[rid]
        if row["grams_available"] < need:
            shortfalls.append(f"needs {_g(need)} g of {row['name']}, "
                              f"have {_g(row['grams_available'])} g")
    for label in not_in_pantry:
        shortfalls.append(f"{label} is not in your pantry")

    rem = day_status_repo(conn, date.today().isoformat())["remaining"]
    if rem is None:
        fit, msgs = "no-target", ()
    else:
        assess = assess_day(rem - total)
        fit, msgs = assess.status, tuple(assess.messages)

    shown = Macros(round(total.kcal, 1), round(total.protein_g, 1),
                   round(total.carbs_g, 1), round(total.fat_g, 1))
    return DishCheck(name, True, None, shown, fit, msgs, not shortfalls, tuple(shortfalls))


SLOTS = ("sweet", "savory", "breakfast")
NOTHING_FITS = ("Nothing in your pantry fits what's left today — want to create your own, "
                "or try a different taste?")


def _fit_score(check: DishCheck) -> float:
    """Rank keepable dishes by protein delivered (higher is better); others get -inf."""
    keepable = (check.ok and check.pantry_ok and check.fit in ("ok", "no-target")
                and check.macros is not None)
    return check.macros.protein_g if keepable else float("-inf")


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

    def suggest_meals(ti):
        try:
            if not isinstance(ti, dict):
                return "Error: invalid input."
            dishes = ti.get("dishes")
            if not isinstance(dishes, list) or not 1 <= len(dishes) <= 3:
                return "Error: 'dishes' must be a list of 1-3 dishes."
            for d in dishes:
                if not isinstance(d, dict):
                    return "Error: each dish must be an object."
                if not isinstance(d.get("name"), str) or not d["name"].strip():
                    return "Error: each dish needs a 'name'."
                if d.get("slot") not in SLOTS:
                    return "Error: each dish 'slot' must be one of sweet, savory, breakfast."
                if not isinstance(d.get("ingredients"), list) or not d["ingredients"]:
                    return "Error: each dish needs a non-empty 'ingredients' list."

            best: dict[str, DishCheck] = {}
            for d in dishes:
                c = validate_dish(d["name"].strip(), d["ingredients"], conn=conn,
                                  food_service=food_service, pantry_store=pantry_store)
                # Keep only resolved + pantry-ready + fitting dishes (-inf otherwise).
                if _fit_score(c) == float("-inf"):
                    continue
                cur = best.get(d["slot"])
                if cur is None or _fit_score(c) > _fit_score(cur):
                    best[d["slot"]] = c
            if not best:
                return NOTHING_FITS

            rem = day_status_repo(conn, date.today().isoformat())["remaining"]
            lines = ["Here's what fits from your pantry (nothing logged yet):"]
            for slot in SLOTS:
                c = best.get(slot)
                if c is None:
                    continue
                m = c.macros
                if c.fit == "ok" and rem is not None:
                    note = f"ready to cook; fits with {rem.kcal - m.kcal:.0f} kcal headroom"
                else:
                    note = "ready to cook; no daily target set"
                lines.append(f"- {slot}: {c.name} — {m.kcal:.0f} kcal, {m.protein_g:.1f}g "
                             f"protein, {m.carbs_g:.1f}g carbs, {m.fat_g:.1f}g fat ({note})")
            return "\n".join(lines)
        except Exception as exc:
            return f"Error: couldn't suggest meals ({exc})"

    return {"check_dish": check_dish, "suggest_meals": suggest_meals}
