"""Pantry tools: pantry_add, pantry_list, pantry_set_quantity, pantry_remove."""

import math

from nutrition_core.food_service import FoodLookupError
from nutrition_core.models import Macros

_MACRO_FIELDS = ("kcal_per_100g", "protein_per_100g", "carbs_per_100g", "fat_per_100g")

TOOLS = [
    {
        "name": "pantry_add",
        "description": (
            "Add stock to the pantry (food on hand). Always give grams plus ONE way to "
            "identify the food: 'barcode' (looked up in the food database), 'name' "
            "(searched; the best match is used), or MANUAL: 'name' plus all four of "
            "kcal_per_100g, protein_per_100g, carbs_per_100g, fat_per_100g. Adding a "
            "food already in the pantry tops up its quantity."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "grams": {"type": "number", "description": "Grams to add (> 0)."},
                "barcode": {"type": "string", "description": "Product barcode (EAN/UPC)."},
                "name": {"type": "string", "description": "Food name (search, or manual entry)."},
                "kcal_per_100g": {"type": "number", "description": "Manual: kcal per 100g."},
                "protein_per_100g": {"type": "number", "description": "Manual: protein g per 100g."},
                "carbs_per_100g": {"type": "number", "description": "Manual: carbs g per 100g."},
                "fat_per_100g": {"type": "number", "description": "Manual: fat g per 100g."},
            },
            "required": ["grams"],
        },
    },
    {
        "name": "pantry_list",
        "description": "List everything in the pantry with quantities in grams.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "pantry_set_quantity",
        "description": (
            "Set the exact grams on hand for a pantry item (use pantry_list for ids). "
            "Grams must be > 0; use pantry_remove to delete an item."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pantry_id": {"type": "integer", "description": "Pantry item id."},
                "grams": {"type": "number", "description": "New quantity in grams (> 0)."},
            },
            "required": ["pantry_id", "grams"],
        },
    },
    {
        "name": "pantry_remove",
        "description": "Remove an item from the pantry entirely (use pantry_list for ids).",
        "input_schema": {
            "type": "object",
            "properties": {"pantry_id": {"type": "integer", "description": "Pantry item id."}},
            "required": ["pantry_id"],
        },
    },
]


def _num(v):
    """Finite non-bool number -> float, else None."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    v = float(v)
    return v if math.isfinite(v) else None


def _id(v):
    if isinstance(v, bool) or not isinstance(v, int):
        return None
    return v


def _g(x: float) -> str:
    return f"{x:g}"


def handlers(pantry_store, food_service):
    def pantry_add(ti):
        try:
            grams = _num(ti.get("grams"))
            if grams is None or grams <= 0:
                return "Error: 'grams' must be a number greater than 0."

            barcode = ti.get("barcode")
            name = ti.get("name")
            has_barcode = isinstance(barcode, str) and barcode.strip() != ""
            has_name = isinstance(name, str) and name.strip() != ""
            macro_vals = [ti.get(f) for f in _MACRO_FIELDS]
            any_macro = any(v is not None for v in macro_vals)

            if has_barcode and (has_name or any_macro):
                return "Error: give either a 'barcode' or a 'name' (with optional per-100g values), not both."

            if has_barcode:
                if food_service is None:
                    return ("Sorry, food lookup isn't available — add it manually "
                            "with per-100g values.")
                try:
                    item = food_service.by_barcode(barcode.strip())
                except FoodLookupError:
                    return "Sorry, I couldn't reach the food database right now. Try again shortly."
                if item is None:
                    return f"I couldn't find barcode {barcode.strip()} in the food database. Nothing added."
                return _store(item.name, grams, item.per100g, item.source.value,
                              item.external_id, item.confidence.value)

            if not has_name:
                return ("Error: provide a 'barcode', a 'name', or a name with all four "
                        "per-100g values (kcal, protein, carbs, fat).")
            name = name.strip()

            if any_macro:  # MANUAL mode
                nums = [_num(v) for v in macro_vals]
                if any(n is None for n in nums):
                    return ("Error: manual entry needs all four per-100g values "
                            "(kcal, protein, carbs, fat) as numbers.")
                if any(n < 0 for n in nums):
                    return "Error: per-100g values can't be negative."
                return _store(name, grams, Macros(*nums), "manual", None, "medium")

            if food_service is None:
                return ("Sorry, food lookup isn't available — add it manually "
                        "with per-100g values.")
            try:
                items = food_service.lookup(name)
            except FoodLookupError:
                return "Sorry, I couldn't reach the food database right now. Try again shortly."
            if not items:
                return f"I couldn't find '{name}' in the food database. Nothing added."
            best = items[0]
            msg = _store(best.name, grams, best.per100g, best.source.value,
                         best.external_id, best.confidence.value)
            if len(items) > 1:
                msg += f" (Used the best match for '{name}' out of {len(items)} results.)"
            return msg
        except Exception as exc:
            return f"Error: couldn't add to the pantry ({exc})"

    def _store(name, grams, per100g, source, external_id, confidence):
        pantry_store.add_or_topup(
            name=name, grams=grams, per100g=per100g, source=source,
            external_id=external_id, confidence=confidence,
        )
        tag = f" ({source}:{external_id})" if external_id else f" ({source})"
        return f"Added {_g(grams)} g of {name}{tag}."

    def pantry_list(ti):
        try:
            items = pantry_store.list_items()
            if not items:
                return "Your pantry is empty."
            return "\n".join(
                f"[{r['id']}] {r['name']} — {_g(r['grams_available'])} g — {r['source']}"
                for r in items
            )
        except Exception as exc:
            return f"Error: couldn't read the pantry ({exc})"

    def _exists(pid):
        return any(r["id"] == pid for r in pantry_store.list_items())

    def pantry_set_quantity(ti):
        try:
            pid = _id(ti.get("pantry_id"))
            if pid is None:
                return "Error: 'pantry_id' must be an integer."
            grams = _num(ti.get("grams"))
            if grams is None:
                return "Error: 'grams' must be a number."
            if grams < 0:
                return "Error: 'grams' can't be negative."
            if grams == 0:
                return "Error: 'grams' can't be 0 — use pantry_remove to delete the item."
            if not _exists(pid):
                return f"No pantry item with id {pid}."
            pantry_store.set_quantity(pid, grams)
            return f"Set pantry item {pid} to {_g(grams)} g."
        except Exception as exc:
            return f"Error: couldn't update the pantry ({exc})"

    def pantry_remove(ti):
        try:
            pid = _id(ti.get("pantry_id"))
            if pid is None:
                return "Error: 'pantry_id' must be an integer."
            if not _exists(pid):
                return f"No pantry item with id {pid}."
            pantry_store.remove(pid)
            return f"Removed pantry item {pid}."
        except Exception as exc:
            return f"Error: couldn't remove from the pantry ({exc})"

    return {
        "pantry_add": pantry_add,
        "pantry_list": pantry_list,
        "pantry_set_quantity": pantry_set_quantity,
        "pantry_remove": pantry_remove,
    }
