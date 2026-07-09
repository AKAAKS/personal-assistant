"""Meal tracking tools: log_meal / meal_summary / export_meals."""

TOOLS = [
    {
        "name": "log_meal",
        "description": "Log a meal with estimated macronutrients. Estimate the macros yourself from the description; state portion assumptions in `assumptions` and your confidence honestly. These are estimates for trend tracking, not a food database.",
        "input_schema": {
            "type": "object",
            "properties": {
                "description": {
                    "type": "string",
                    "description": "What was eaten (e.g., 'Grilled chicken with rice and broccoli').",
                },
                "calories": {
                    "type": "integer",
                    "description": "Estimated total calories.",
                },
                "protein_g": {
                    "type": "number",
                    "description": "Estimated protein in grams.",
                },
                "carbs_g": {
                    "type": "number",
                    "description": "Estimated carbohydrates in grams.",
                },
                "fat_g": {
                    "type": "number",
                    "description": "Estimated fat in grams.",
                },
                "confidence": {
                    "type": "string",
                    "enum": ["low", "medium", "high"],
                    "description": "Your confidence in the estimate (low/medium/high).",
                },
                "quantity": {
                    "type": "string",
                    "description": "Portion size or quantity (e.g., '1 plate', '2 cups'). Optional.",
                },
                "assumptions": {
                    "type": "string",
                    "description": "Notes on assumptions made (e.g., 'estimated from visual inspection'). Optional.",
                },
            },
            "required": ["description", "calories", "protein_g", "carbs_g", "fat_g", "confidence"],
        },
    },
    {
        "name": "meal_summary",
        "description": "Get today's meal summary: total meals, calories, and macros.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "export_meals",
        "description": "Export all meal logs to a CSV or JSON file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "format": {
                    "type": "string",
                    "enum": ["csv", "json"],
                    "description": "Export format: 'csv' or 'json'. Default: 'csv'.",
                },
            },
            "required": [],
        },
    },
]


def handlers(meal_store):
    """Meal tool handlers closing over meal_store.

    Args:
        meal_store: MealStore instance

    Returns:
        dict: tool name -> callable(tool_input: dict) -> str
    """

    def log_meal(ti):
        """Log a meal with macronutrient estimates."""
        description = ti.get("description", "").strip()
        calories = ti.get("calories")
        protein_g = ti.get("protein_g")
        carbs_g = ti.get("carbs_g")
        fat_g = ti.get("fat_g")
        confidence = ti.get("confidence", "").strip()
        quantity = ti.get("quantity", "").strip()
        assumptions = ti.get("assumptions", "").strip()

        # Validate required fields
        if not description:
            return "Error: description is required."
        if calories is None:
            return "Error: calories is required."
        if protein_g is None:
            return "Error: protein_g is required."
        if carbs_g is None:
            return "Error: carbs_g is required."
        if fat_g is None:
            return "Error: fat_g is required."
        if not confidence:
            return "Error: confidence is required."

        # Validate confidence enum
        if confidence not in ("low", "medium", "high"):
            return f"Error: confidence must be 'low', 'medium', or 'high', got {confidence!r}."

        try:
            meal_id = meal_store.add(
                description, quantity, calories, protein_g, carbs_g, fat_g, confidence, assumptions
            )
            return f"Meal logged (#{meal_id}): {description} — {calories} kcal"
        except Exception as exc:
            return f"Error: {exc}"

    def meal_summary(ti):
        """Summarize today's meal log."""
        summary = meal_store.today_summary()
        meals = summary["meals"]

        if not meals:
            return "No meals logged today."

        lines = []
        total_cals = summary["calories"]
        total_protein = summary["protein_g"]
        total_carbs = summary["carbs_g"]
        total_fat = summary["fat_g"]

        lines.append(
            f"Today: {len(meals)} meals, {total_cals} kcal, {total_protein:.1f}g protein, "
            f"{total_carbs:.1f}g carbs, {total_fat:.1f}g fat"
        )

        # Show each meal with its real DB id (not enumerate position)
        for meal in meals:
            meal_id = meal["id"]
            desc = meal["description"]
            cal = meal["calories"]
            lines.append(f"{meal_id}. {desc} — {cal} kcal")

        return "\n".join(lines)

    def export_meals(ti):
        """Export meal logs to file."""
        fmt = ti.get("format", "csv").strip().lower()

        if fmt not in ("csv", "json"):
            return f"Error: format must be 'csv' or 'json', got {fmt!r}."

        try:
            path = meal_store.export(fmt)
            return f"Exported meal log to {path}"
        except ValueError as exc:
            return f"Error: {exc}"
        except Exception as exc:
            return f"Error: {exc}"

    return {
        "log_meal": log_meal,
        "meal_summary": meal_summary,
        "export_meals": export_meals,
    }
