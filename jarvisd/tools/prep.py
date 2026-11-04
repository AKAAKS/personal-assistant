"""Meal-prep tools: plan_prep (lock dishes as `planned` meals + shopping delta).

The LLM never supplies macros. Every ingredient is resolved to a real snapshot
(reusing meal_log.resolve_ingredient) and the engine computes the totals.
Planning does NOT touch today's budget (planned meals are not consumed) and
NEVER depletes the pantry; it only reports what is missing.
"""

from datetime import date as _date

from jarvisd.tools.meal_log import resolve_ingredient
from nutrition_core.engine import compute_ingredient, compute_meal
from nutrition_core.store.meals import log_verified_meal

TOOLS = [
    {
        "name": "plan_prep",
        "description": (
            "Lock specific dishes for upcoming day(s) as PLANNED meals (they do not use "
            "today's budget and do not touch pantry stock) and get back a shopping list: "
            "what the plan needs minus what the pantry holds. Give 'dishes': each has "
            "'name', 'date' (YYYY-MM-DD) and 'ingredients' (each 'grams' plus exactly ONE "
            "of 'barcode', 'name' or 'pantry_id'). Macros are computed from real food data. "
            "A dish with an unresolvable ingredient is skipped and reported."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "dishes": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "date": {"type": "string", "description": "YYYY-MM-DD."},
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
                        "required": ["name", "date", "ingredients"],
                    },
                },
            },
            "required": ["dishes"],
        },
    },
]


def _g(x: float) -> str:
    return f"{x:g}"


def _key(r):
    if r.pantry_id is not None:
        return ("pid", r.pantry_id)
    if r.external_id:
        return ("food", r.source, r.external_id)
    return ("name", r.meal_ingredient.name.strip().lower())


def _shopping_delta(resolved_dishes, pantry_store) -> list[dict]:
    """Aggregate grams needed per food across all dishes minus pantry stock.

    `resolved_dishes` is a list of lists of ResolvedIngredient. Only items with
    a positive shortfall are returned: {"name","need_g","have_g","buy_g"}.
    """
    items = pantry_store.list_items()
    by_id = {row["id"]: row for row in items}
    need: dict[tuple, float] = {}
    rows: dict[tuple, dict | None] = {}
    label: dict[tuple, str] = {}
    for dish in resolved_dishes:
        for r in dish:
            # Normalize to the pantry row when one exists, so every identifier
            # form (pantry_id / barcode / name) of the same food shares a bucket.
            row = None
            if r.pantry_id is not None:
                row = by_id.get(r.pantry_id)
            elif r.external_id:
                row = pantry_store.find_by_food(r.source, r.external_id)
            if row is not None:
                k = ("pid", row["id"])
            elif r.external_id:
                k = ("food", r.source, r.external_id)
            else:
                k = ("name", r.meal_ingredient.name.strip().lower())
                row = next((i for i in items if i["name"].strip().lower() == k[1]), None)
                if row is not None:
                    k = ("pid", row["id"])
            need[k] = need.get(k, 0.0) + r.meal_ingredient.grams
            rows.setdefault(k, row)
            label.setdefault(k, r.meal_ingredient.name)

    out = []
    for k, n in need.items():
        row = rows[k]
        have = float(row["grams_available"]) if row else 0.0
        buy = max(0.0, n - have)
        if buy > 0:
            out.append({"name": row["name"] if row else label[k],
                        "need_g": n, "have_g": have, "buy_g": buy})
    return out


def handlers(conn, food_service, pantry_store):
    def plan_prep(ti):
        try:
            if not isinstance(ti, dict):
                return "Error: invalid input."
            dishes = ti.get("dishes")
            if not isinstance(dishes, list) or not dishes:
                return "Error: 'dishes' must be a non-empty list."
            for d in dishes:
                if not isinstance(d, dict):
                    return "Error: each dish must be an object."
                if not isinstance(d.get("name"), str) or not d["name"].strip():
                    return "Error: each dish needs a 'name'."
                if not isinstance(d.get("date"), str) or not d["date"].strip():
                    return "Error: each dish needs a 'date' (YYYY-MM-DD)."
                if not isinstance(d.get("ingredients"), list) or not d["ingredients"]:
                    return "Error: each dish needs a non-empty 'ingredients' list."

            locked, skipped, resolved_locked = [], [], []
            for d in dishes:
                name, day = d["name"].strip(), d["date"].strip()
                try:
                    _date.fromisoformat(day)
                except ValueError:
                    skipped.append((name, f"date {day!r} is not YYYY-MM-DD."))
                    continue
                resolved, err = [], None
                for spec in d["ingredients"]:
                    r = resolve_ingredient(spec, food_service=food_service,
                                           pantry_store=pantry_store)
                    if isinstance(r, str):
                        err = r
                        break
                    resolved.append(r)
                if err is not None:
                    skipped.append((name, err))
                    continue
                total = compute_meal([
                    compute_ingredient(r.meal_ingredient.name, r.meal_ingredient.grams,
                                       r.meal_ingredient.per100g, r.meal_ingredient.source)
                    for r in resolved]).total
                # Planned only: never depletes the pantry.
                log_verified_meal(
                    conn, name=name, date=day,
                    ingredients=[r.meal_ingredient for r in resolved],
                    status="planned",
                    links=[(r.external_id, r.pantry_id) for r in resolved])
                locked.append((name, day, total))
                resolved_locked.append(resolved)

            lines = []
            if locked:
                lines.append("Locked (planned, not eaten; pantry unchanged):")
                for name, day, m in locked:
                    lines.append(f"- {name} on {day}: {m.kcal:.0f} kcal, {m.protein_g:.1f}g "
                                 f"protein, {m.carbs_g:.1f}g carbs, {m.fat_g:.1f}g fat")
            if skipped:
                lines.append("Skipped (NOT locked):")
                for name, why in skipped:
                    lines.append(f"- {name}: {why}")
            if locked:
                delta = _shopping_delta(resolved_locked, pantry_store)
                if delta:
                    lines.append("Shopping list:")
                    for it in delta:
                        lines.append(f"- {it['name']}: buy {_g(it['buy_g'])} g "
                                     f"(need {_g(it['need_g'])} g, have {_g(it['have_g'])} g)")
                else:
                    lines.append("Nothing to buy — your pantry covers this plan.")
            return "\n".join(lines)
        except Exception as exc:
            return f"Error: couldn't plan the prep ({exc})"

    return {"plan_prep": plan_prep}
