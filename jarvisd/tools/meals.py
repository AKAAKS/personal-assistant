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
    {
        "name": "set_nutrition_targets",
        "description": (
            "Set the user's daily nutrition targets (calories + macros). Once set, "
            "meal_summary shows progress toward them."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "calories": {"type": "integer", "description": "Daily calorie target."},
                "protein_g": {"type": "number", "description": "Daily protein target (g)."},
                "carbs_g": {"type": "number", "description": "Daily carbs target (g)."},
                "fat_g": {"type": "number", "description": "Daily fat target (g)."},
            },
            "required": ["calories", "protein_g", "carbs_g", "fat_g"],
        },
    },
    {
        "name": "meal_trends",
        "description": "Show a multi-day trend of calories/macros per day (default last 7 days).",
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "description": "How many days back to include (default 7)."},
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

    def _is_number(value):
        """True for int/float but NOT bool (isinstance(True, int) is True in Python)."""
        return isinstance(value, (int, float)) and not isinstance(value, bool)

    def log_meal(ti):
        """Log a meal with macronutrient estimates."""
        description = ti.get("description")
        calories = ti.get("calories")
        protein_g = ti.get("protein_g")
        carbs_g = ti.get("carbs_g")
        fat_g = ti.get("fat_g")
        confidence = ti.get("confidence")
        quantity = ti.get("quantity", "")
        assumptions = ti.get("assumptions", "")

        # Validate types before touching the store — bad values must never persist.
        if not isinstance(description, str) or not description.strip():
            return "Error: description is required and must be a non-empty string."
        if not _is_number(calories):
            return "Error: calories is required and must be a number."
        if not _is_number(protein_g):
            return "Error: protein_g is required and must be a number."
        if not _is_number(carbs_g):
            return "Error: carbs_g is required and must be a number."
        if not _is_number(fat_g):
            return "Error: fat_g is required and must be a number."

        # Validate confidence enum
        if confidence not in ("low", "medium", "high"):
            return f"Error: confidence must be 'low', 'medium', or 'high', got {confidence!r}."

        description = description.strip()
        quantity = quantity.strip() if isinstance(quantity, str) else ""
        assumptions = assumptions.strip() if isinstance(assumptions, str) else ""

        try:
            meal_id = meal_store.add(
                description, quantity, calories, protein_g, carbs_g, fat_g, confidence, assumptions
            )
            return f"Meal logged (#{meal_id}): {description} — {calories} kcal"
        except Exception as exc:
            return f"Error: {exc}"

    def meal_summary(ti):
        """Summarize today's meal log."""
        # The never-raise guarantee must hold even against bad rows already in
        # the DB (SQLite's flexible typing lets TEXT into INTEGER columns).
        try:
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
                f"Today: {len(meals)} meals, {total_cals} kcal, {total_protein:.1f} g protein, "
                f"{total_carbs:.1f} g carbs, {total_fat:.1f} g fat"
            )

            # If daily targets are set, show progress toward them.
            try:
                targets = meal_store.get_targets()
            except Exception:
                targets = None
            if targets:
                def _pct(got, goal):
                    return f"{(100 * got / goal):.0f}%" if goal else "—"
                lines.append(
                    f"vs target: {total_cals}/{targets['calories']} kcal ({_pct(total_cals, targets['calories'])}), "
                    f"protein {total_protein:.0f}/{targets['protein_g']:.0f} g, "
                    f"carbs {total_carbs:.0f}/{targets['carbs_g']:.0f} g, "
                    f"fat {total_fat:.0f}/{targets['fat_g']:.0f} g"
                )

            # Show each meal with its real DB id (not enumerate position)
            for meal in meals:
                meal_id = meal["id"]
                desc = meal["description"]
                cal = meal["calories"]
                lines.append(f"{meal_id}. {desc} — {cal} kcal")

            return "\n".join(lines)
        except Exception as exc:
            return f"Error reading meal log: {exc}"

    def export_meals(ti):
        """Export meal logs to file."""
        fmt = ti.get("format", "csv")
        if isinstance(fmt, str):
            fmt = fmt.strip().lower()

        # Format validation lives in ONE place: MealStore.export raises
        # ValueError for anything but csv/json.
        try:
            path = meal_store.export(fmt)
            return f"Exported meal log to {path}"
        except ValueError as exc:
            return f"Error: {exc}"
        except Exception as exc:
            return f"Error: {exc}"

    def set_nutrition_targets(ti):
        """Set the user's daily calorie + macro targets."""
        calories = ti.get("calories")
        protein_g = ti.get("protein_g")
        carbs_g = ti.get("carbs_g")
        fat_g = ti.get("fat_g")
        for name, val in (("calories", calories), ("protein_g", protein_g),
                          ("carbs_g", carbs_g), ("fat_g", fat_g)):
            if not _is_number(val):
                return f"Error: {name} is required and must be a number."
        try:
            meal_store.set_targets(int(calories), protein_g, carbs_g, fat_g)
        except Exception as exc:
            return f"Error: {exc}"
        return (
            f"Daily targets set: {int(calories)} kcal, {protein_g:.0f} g protein, "
            f"{carbs_g:.0f} g carbs, {fat_g:.0f} g fat."
        )

    def meal_trends(ti):
        """Show per-day totals over a recent window (default 7 days)."""
        days = ti.get("days")
        if not isinstance(days, int) or isinstance(days, bool) or days <= 0:
            days = 7
        try:
            rows = meal_store.range_summary(days=days)
        except Exception as exc:
            return f"Error reading meal log: {exc}"
        if not rows:
            return f"No meals logged in the last {days} day(s)."
        lines = [f"Last {days} days:"]
        for r in rows:
            lines.append(
                f"{r['day']}: {r['meals']} meals, {r['calories']} kcal, "
                f"{r['protein_g']:.0f} g protein, {r['carbs_g']:.0f} g carbs, {r['fat_g']:.0f} g fat"
            )
        n = len(rows)
        avg_cal = sum(r["calories"] for r in rows) / n
        avg_protein = sum(r["protein_g"] for r in rows) / n
        lines.append(f"Average over {n} logged day(s): {avg_cal:.0f} kcal, {avg_protein:.0f} g protein")
        return "\n".join(lines)

    return {
        "log_meal": log_meal,
        "meal_summary": meal_summary,
        "export_meals": export_meals,
        "set_nutrition_targets": set_nutrition_targets,
        "meal_trends": meal_trends,
    }
