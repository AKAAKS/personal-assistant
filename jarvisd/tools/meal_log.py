"""Meal logging tools: log_meal (verified, DB-sourced macros + pantry depletion).

Zero-error rule: the LLM never supplies a macro total. Every ingredient is
resolved to a real per-100g snapshot first; the engine computes the totals.
Ordering is load-bearing: resolve ALL -> (any failure: log nothing) -> log ->
best-effort deplete (a depletion failure never un-logs the meal).
"""

import math
from dataclasses import dataclass
from datetime import date

from nutrition_core.engine import compute_ingredient, compute_meal
from nutrition_core.food_service import FoodLookupError
from nutrition_core.models import FoodSource, Macros, MealIngredient
from nutrition_core.store.meals import day_status as day_status_repo
from nutrition_core.store.meals import log_verified_meal

_KEYS = ("barcode", "name", "pantry_id")

TOOLS = [
    {
        "name": "log_meal",
        "description": (
            "Log a meal you ate. Macros are computed from real food data, never from "
            "your guess. Give the dish 'name' and 'ingredients': each has 'grams' plus "
            "exactly ONE of 'barcode', 'name' (searched in the food database), or "
            "'pantry_id' (an item from the pantry; use pantry_list for ids). If any "
            "ingredient can't be resolved, nothing is logged. Pantry stock is "
            "depleted for ingredients found in the pantry."
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
        "name": "log_estimated_meal",
        "description": (
            "Log a meal eaten OUT (restaurant/takeaway) where portions are eyeballed. "
            "Same ingredient shape as log_meal ('grams' + one of 'barcode'/'name'/"
            "'pantry_id'). Macros come from the food database but are flagged as "
            "ESTIMATES. Never changes pantry stock. Optional 'confidence': low (default) "
            "or medium."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "description": {"type": "string", "description": "What was eaten."},
                "ingredients": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "properties": {
                            "grams": {"type": "number", "description": "Estimated grams (> 0)."},
                            "barcode": {"type": "string"},
                            "name": {"type": "string"},
                            "pantry_id": {"type": "integer"},
                        },
                        "required": ["grams"],
                    },
                },
                "confidence": {"type": "string", "enum": ["low", "medium"]},
            },
            "required": ["description", "ingredients"],
        },
    },
    {
        "name": "day_status",
        "description": (
            "Today's nutrition status: daily target, consumed (split into verified vs "
            "estimated), remaining, and an honest over/cornered warning. Optional "
            "'date' (YYYY-MM-DD, default today)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"date": {"type": "string", "description": "YYYY-MM-DD."}},
        },
    },
]


@dataclass(frozen=True)
class ResolvedIngredient:
    meal_ingredient: MealIngredient
    source: str
    external_id: str | None
    pantry_id: int | None


def _num(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    v = float(v)
    return v if math.isfinite(v) else None


def _g(x: float) -> str:
    return f"{x:g}"


def _present(v) -> bool:
    if v is None:
        return False
    if isinstance(v, str):
        return v.strip() != ""
    return True


def _macro_line(m) -> str:
    return (f"{m.kcal:,.0f} kcal, {m.protein_g:.0f}g protein, "
            f"{m.carbs_g:.0f}g carbs, {m.fat_g:.0f}g fat.")


def resolve_ingredient(spec, *, food_service, pantry_store):
    """Resolve one ingredient spec to a ResolvedIngredient, or an error string."""
    try:
        if not isinstance(spec, dict):
            return "Error: each ingredient must be an object with 'grams' and a barcode, name or pantry_id."
        label = next((str(spec[k]) for k in ("name", "barcode", "pantry_id")
                      if _present(spec.get(k))), "?")
        grams = _num(spec.get("grams"))
        if grams is None or grams <= 0:
            return f"Error: ingredient '{label}': 'grams' must be a number greater than 0."
        given = [k for k in _KEYS if _present(spec.get(k))]
        if len(given) != 1:
            return (f"Error: ingredient '{label}': give exactly one of 'barcode', "
                    "'name' or 'pantry_id'.")
        key = given[0]

        if key == "pantry_id":
            pid = spec["pantry_id"]
            if isinstance(pid, bool) or not isinstance(pid, int):
                return f"Error: ingredient '{label}': 'pantry_id' must be an integer."
            row = next((r for r in pantry_store.list_items() if r["id"] == pid), None)
            if row is None:
                return f"Error: no pantry item with id {pid}; nothing logged."
            per100g = Macros(row["per100_kcal"], row["per100_protein_g"],
                             row["per100_carbs_g"], row["per100_fat_g"])
            return ResolvedIngredient(
                MealIngredient(row["name"], grams, per100g, FoodSource(row["source"])),
                row["source"], row["external_id"], pid)

        if food_service is None:
            return (f"Error: ingredient '{label}': food lookup isn't available; "
                    "use a pantry_id instead.")
        try:
            if key == "barcode":
                item = food_service.by_barcode(str(spec["barcode"]).strip())
                miss = f"Error: couldn't find barcode {label} in the food database; nothing logged."
            else:
                items = food_service.lookup(str(spec["name"]).strip())
                item = items[0] if items else None
                miss = f"Error: couldn't find '{label}' in the food database; nothing logged."
        except FoodLookupError:
            return ("Sorry, I couldn't reach the food database right now "
                    f"(ingredient '{label}'). Nothing logged.")
        if item is None:
            return miss
        return ResolvedIngredient(
            MealIngredient(item.name, grams, item.per100g, item.source),
            item.source.value, item.external_id, None)
    except Exception as exc:
        return f"Error: couldn't resolve ingredient ({exc}); nothing logged."


def handlers(conn, food_service, pantry_store):
    def log_meal(ti):
        try:
            if not isinstance(ti, dict):
                return "Error: invalid input; nothing logged."
            name = ti.get("name")
            if not isinstance(name, str) or not name.strip():
                return "Error: 'name' (the dish name) is required; nothing logged."
            ings = ti.get("ingredients")
            if not isinstance(ings, list) or not ings:
                return "Error: 'ingredients' must be a non-empty list; nothing logged."

            # 1. resolve ALL first; any failure aborts the whole meal.
            resolved = []
            for spec in ings:
                r = resolve_ingredient(spec, food_service=food_service,
                                       pantry_store=pantry_store)
                if isinstance(r, str):
                    return r
                resolved.append(r)

            # 2. compute the displayed total BEFORE persisting, so no fallible
            #    work remains after the row exists (no error -> no retry -> no
            #    duplicate log), then log (engine-computed totals).
            total = compute_meal([
                compute_ingredient(r.meal_ingredient.name, r.meal_ingredient.grams,
                                   r.meal_ingredient.per100g, r.meal_ingredient.source)
                for r in resolved]).total
            today = date.today().isoformat()
            log_verified_meal(
                conn, name=name.strip(), date=today,
                ingredients=[r.meal_ingredient for r in resolved],
                status="verified", confidence="high")

            # 3. best-effort depletion; never un-logs.
            depleted, warnings = [], []
            try:
                for r in resolved:
                    grams = r.meal_ingredient.grams
                    try:
                        if r.pantry_id is not None:
                            pantry_store.deplete_by_id(r.pantry_id, grams)
                            depleted.append(f"{_g(grams)} g {r.meal_ingredient.name}")
                        elif r.external_id is not None:
                            left = pantry_store.deplete(
                                source=r.source, external_id=r.external_id, grams=grams)
                            if left is not None:
                                depleted.append(f"{_g(grams)} g {r.meal_ingredient.name}")
                    except Exception as exc:
                        warnings.append(f"couldn't update pantry for {r.meal_ingredient.name} ({exc})")
            except Exception as exc:
                warnings.append(f"pantry update failed ({exc})")

            msg = (f"Logged '{name.strip()}': {total.kcal:.0f} kcal, "
                   f"{total.protein_g:.1f}g protein, {total.carbs_g:.1f}g carbs, "
                   f"{total.fat_g:.1f}g fat.")
            msg += (" Pantry depleted: " + ", ".join(depleted) + "."
                    if depleted else " No pantry stock changed.")
            if warnings:
                msg += " Warning: meal is logged, but " + "; ".join(warnings) + "."
            return msg
        except Exception as exc:
            return f"Error: couldn't log the meal ({exc})"

    def log_estimated_meal(ti):
        try:
            if not isinstance(ti, dict):
                return "Error: invalid input; nothing logged."
            desc = ti.get("description")
            if not isinstance(desc, str) or not desc.strip():
                return "Error: 'description' is required; nothing logged."
            ings = ti.get("ingredients")
            if not isinstance(ings, list) or not ings:
                return "Error: 'ingredients' must be a non-empty list; nothing logged."
            conf = ti.get("confidence")
            if conf not in ("low", "medium"):
                conf = "low"

            resolved = []
            for spec in ings:
                r = resolve_ingredient(spec, food_service=food_service,
                                       pantry_store=pantry_store)
                if isinstance(r, str):
                    return r
                resolved.append(r)

            # Estimated meals NEVER touch the pantry: no deplete calls here.
            log_verified_meal(
                conn, name=desc.strip(), date=date.today().isoformat(),
                ingredients=[r.meal_ingredient for r in resolved],
                status="estimated", confidence=conf,
                assumptions="estimated portions")
            total = compute_meal([
                compute_ingredient(r.meal_ingredient.name, r.meal_ingredient.grams,
                                   r.meal_ingredient.per100g, r.meal_ingredient.source)
                for r in resolved]).total
            return (f"Logged '{desc.strip()}' as an ESTIMATE ({conf} confidence): "
                    f"~{total.kcal:.0f} kcal, {total.protein_g:.1f}g protein, "
                    f"{total.carbs_g:.1f}g carbs, {total.fat_g:.1f}g fat. These macros are "
                    "estimates, not verified. Pantry not changed.")
        except Exception as exc:
            return f"Error: couldn't log the estimated meal ({exc})"

    def day_status(ti):
        try:
            d = ti.get("date") if isinstance(ti, dict) else None
            if not isinstance(d, str) or not d.strip():
                d = date.today().isoformat()
            d = d.strip()
            st = day_status_repo(conn, d)
            target, ver, est, con = st["target"], st["verified"], st["estimated"], st["consumed"]
            rem, assess = st["remaining"], st["assessment"]
            if target is None and con.kcal == 0 and con.protein_g == 0                     and con.carbs_g == 0 and con.fat_g == 0:
                return "No daily target set. Nothing logged yet today."
            lines = []
            if target is None:
                lines.append("No daily target set.")
            else:
                lines.append(f"Target: {_macro_line(target)}")
            lines.append(f"Consumed: {con.kcal:,.0f} kcal "
                         f"({ver.kcal:,.0f} verified + {est.kcal:,.0f} estimated); "
                         f"{con.protein_g:.0f}g protein, {con.carbs_g:.0f}g carbs, "
                         f"{con.fat_g:.0f}g fat.")
            if est.kcal > 0:
                lines.append("Note: the estimated part comes from eyeballed portions.")
            if rem is not None:
                lines.append(f"Remaining: {_macro_line(rem)}")
            if assess is not None and assess.status != "ok":
                lines.append(f"Heads up ({assess.status}): " + "; ".join(assess.messages))
            return "\n".join(lines)
        except Exception as exc:
            return f"Error: couldn't get the day status ({exc})"

    return {"log_meal": log_meal, "log_estimated_meal": log_estimated_meal,
            "day_status": day_status}
