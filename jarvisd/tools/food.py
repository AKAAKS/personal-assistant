"""Food database tool: lookup_food."""

from nutrition_core.food_service import FoodLookupError

TOOLS = [
    {
        "name": "lookup_food",
        "description": (
            "Look up a food's accurate per-100g macros (kcal, protein, carbs, fat) "
            "from the food database (USDA / Open Food Facts). Use this when "
            "meal-planning instead of guessing macros. Returns candidate matches "
            "with their source and id."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Food to search for (e.g. 'cooked white rice')."},
                "limit": {"type": "integer", "description": "Max candidates to return. Default 5."},
            },
            "required": ["query"],
        },
    },
]


def handlers(food_service):
    def lookup_food(ti):
        try:
            query = ti.get("query")
            if not isinstance(query, str) or not query.strip():
                return "Error: 'query' must be a non-empty string."
            query = query.strip()
            limit = ti.get("limit", 5)
            if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
                limit = 5
            try:
                items = food_service.lookup(query, limit=limit)
            except FoodLookupError:
                return "Sorry, I couldn't reach the food database right now. Try again shortly."
            if not items:
                return f"No matches for '{query}' in the food database."
            lines = []
            for it in items:
                m = it.per100g
                lines.append(
                    f"{it.name} — {m.kcal:g} kcal / {m.protein_g:g}g P / "
                    f"{m.carbs_g:g}g C / {m.fat_g:g}g F per 100g — "
                    f"{it.source.value}:{it.external_id}"
                )
            return "\n".join(lines)
        except Exception as exc:
            return f"Error: food lookup failed ({exc})"

    return {"lookup_food": lookup_food}
