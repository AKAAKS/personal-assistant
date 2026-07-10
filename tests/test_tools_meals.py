"""Tests for meal tracking tools."""

import sqlite3
from datetime import datetime
from types import SimpleNamespace
from pathlib import Path
import pytest
from jarvisd.db import connect
from jarvisd.meal_store import MealStore
from jarvisd.tools import meals


@pytest.fixture
def conn():
    """In-memory SQLite connection with schema."""
    c = connect(":memory:")
    yield c
    c.close()


@pytest.fixture
def meal_store(conn):
    """MealStore instance."""
    return MealStore(conn)


@pytest.fixture
def meal_handlers(meal_store):
    """Meal tool handlers."""
    return meals.handlers(meal_store)


class TestMealTools:
    """Tests for meal tool definitions and handlers."""

    def test_meal_tools_defined(self):
        """Verify meal tools are defined."""
        tool_names = [t["name"] for t in meals.TOOLS]
        assert "log_meal" in tool_names
        assert "meal_summary" in tool_names
        assert "export_meals" in tool_names

    def test_meal_tools_schema(self):
        """Verify meal tool schemas are well-formed."""
        for tool in meals.TOOLS:
            assert "name" in tool
            assert "description" in tool
            assert "input_schema" in tool
            assert tool["input_schema"]["type"] == "object"

    def test_log_meal_handler_valid(self, meal_handlers, meal_store):
        """Test log_meal handler with valid input."""
        result = meal_handlers["log_meal"]({
            "description": "Grilled chicken and rice",
            "calories": 450,
            "protein_g": 35.0,
            "carbs_g": 45.0,
            "fat_g": 8.0,
            "confidence": "high",
            "quantity": "1 plate",
            "assumptions": "Estimated portion from visual inspection"
        })
        assert "Meal logged" in result or "logged" in result.lower()
        assert "450" in result or "kcal" in result.lower()

    def test_log_meal_handler_no_quantity(self, meal_handlers, meal_store):
        """Test log_meal handler without optional quantity field."""
        result = meal_handlers["log_meal"]({
            "description": "Apple",
            "calories": 95,
            "protein_g": 0.5,
            "carbs_g": 25.0,
            "fat_g": 0.3,
            "confidence": "medium"
        })
        assert "Meal logged" in result or "logged" in result.lower()

    def test_log_meal_handler_missing_required_field(self, meal_handlers):
        """Test log_meal with missing required field returns error."""
        result = meal_handlers["log_meal"]({
            "description": "Test meal",
            "calories": 500
            # Missing protein_g, carbs_g, fat_g, confidence
        })
        assert isinstance(result, str)
        assert "error" in result.lower() or "required" in result.lower()

    def test_log_meal_handler_invalid_confidence(self, meal_handlers):
        """Test log_meal with invalid confidence value."""
        result = meal_handlers["log_meal"]({
            "description": "Test meal",
            "calories": 500,
            "protein_g": 20.0,
            "carbs_g": 60.0,
            "fat_g": 10.0,
            "confidence": "very_high"  # Invalid
        })
        assert isinstance(result, str)
        assert "error" in result.lower() or "confidence" in result.lower()

    def test_log_meal_rejects_non_numeric_calories(self, meal_handlers):
        """log_meal with calories='a lot' must return an Error string and NOT persist."""
        result = meal_handlers["log_meal"]({
            "description": "Mystery meal",
            "calories": "a lot",  # not a number
            "protein_g": 10.0,
            "carbs_g": 10.0,
            "fat_g": 10.0,
            "confidence": "low"
        })
        assert isinstance(result, str)
        assert result.startswith("Error")
        # Must not have been persisted — summary stays empty
        summary = meal_handlers["meal_summary"]({})
        assert "No meals logged" in summary

    def test_log_meal_rejects_bool_calories(self, meal_handlers):
        """log_meal must reject bool for calories (isinstance(True, int) is True in Python)."""
        result = meal_handlers["log_meal"]({
            "description": "Bool meal",
            "calories": True,
            "protein_g": 10.0,
            "carbs_g": 10.0,
            "fat_g": 10.0,
            "confidence": "low"
        })
        assert result.startswith("Error")
        summary = meal_handlers["meal_summary"]({})
        assert "No meals logged" in summary

    def test_log_meal_rejects_non_numeric_macros(self, meal_handlers):
        """log_meal must reject non-numeric protein_g/carbs_g/fat_g (incl. bool)."""
        for field, bad_value in [
            ("protein_g", "lots"),
            ("carbs_g", "some"),
            ("fat_g", True),
        ]:
            ti = {
                "description": "Test meal",
                "calories": 300,
                "protein_g": 10.0,
                "carbs_g": 10.0,
                "fat_g": 10.0,
                "confidence": "medium",
            }
            ti[field] = bad_value
            result = meal_handlers["log_meal"](ti)
            assert result.startswith("Error"), f"{field}={bad_value!r} was accepted"
        summary = meal_handlers["meal_summary"]({})
        assert "No meals logged" in summary

    def test_log_meal_rejects_non_string_description(self, meal_handlers):
        """log_meal must reject a non-string description without raising."""
        result = meal_handlers["log_meal"]({
            "description": 12345,
            "calories": 300,
            "protein_g": 10.0,
            "carbs_g": 10.0,
            "fat_g": 10.0,
            "confidence": "medium"
        })
        assert isinstance(result, str)
        assert result.startswith("Error")

    def test_meal_summary_never_raises_on_bad_row(self, conn, meal_handlers):
        """meal_summary must return an Error string, not raise, when a bad row is
        already in the DB (SQLite's flexible typing lets TEXT into INTEGER columns)."""
        ts = datetime.now().isoformat(timespec="seconds")
        conn.execute(
            "INSERT INTO meals (ts, description, quantity, calories, protein_g, carbs_g, "
            "fat_g, confidence, assumptions) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ts, "bad row", "", "a lot", "x", "y", "z", "low", ""),
        )
        conn.commit()

        result = meal_handlers["meal_summary"]({})
        assert isinstance(result, str)
        assert result.startswith("Error reading meal log")

    def test_meal_summary_handler_empty(self, meal_handlers):
        """Test meal_summary with no meals logged."""
        result = meal_handlers["meal_summary"]({})
        assert "No meals logged" in result or "no meals" in result.lower()

    def test_meal_summary_handler_with_meals(self, meal_handlers, meal_store):
        """Test meal_summary with logged meals."""
        # Log two meals
        meal_store.add("Breakfast: oatmeal", "1 bowl", 200, 6.0, 35.0, 3.0, "medium", "standard portion")
        meal_store.add("Lunch: salad", "large", 350, 15.0, 40.0, 12.0, "high", "weighed ingredients")

        result = meal_handlers["meal_summary"]({})
        # Exact spec template: "Today: N meals, X kcal, P g protein, C g carbs, F g fat"
        assert result.splitlines()[0] == (
            "Today: 2 meals, 550 kcal, 21.0 g protein, 75.0 g carbs, 15.0 g fat"
        )
        assert "oatmeal" in result
        assert "salad" in result

    def test_meal_summary_shows_real_db_ids(self, meal_handlers, meal_store):
        """The listing must show the DB id, not the row's position."""
        id1 = meal_store.add("Meal 1", "1x", 300, 20.0, 35.0, 8.0, "high", "")
        id2 = meal_store.add("Meal 2", "1x", 400, 25.0, 40.0, 10.0, "medium", "")

        result = meal_handlers["meal_summary"]({})
        lines = result.split("\n")
        # First line is the summary, rest are individual meals
        meal_lines = [l for l in lines if l and not l.startswith("Today:")]

        assert len(meal_lines) >= 2
        # Both IDs should appear
        assert f"{id1}." in result
        assert f"{id2}." in result

    def test_export_meals_handler_csv(self, meal_store, tmp_path):
        """Test export_meals handler with CSV format using spy store."""
        # Create a spy store that records export calls
        spy_store = SimpleNamespace()
        spy_store.export_calls = []

        def spy_export(fmt, out_dir="data/exports"):
            spy_store.export_calls.append((fmt, out_dir))
            # Return a fake path
            return f"{out_dir}/meals-20260709-120000.{fmt}"

        spy_store.export = spy_export

        handlers = meals.handlers(spy_store)
        result = handlers["export_meals"]({"format": "csv"})

        assert "Exported" in result or "exported" in result.lower()
        assert "csv" in result.lower()
        assert len(spy_store.export_calls) == 1
        assert spy_store.export_calls[0][0] == "csv"

    def test_export_meals_handler_json(self, meal_store, tmp_path):
        """Test export_meals handler with JSON format."""
        spy_store = SimpleNamespace()
        spy_store.export_calls = []

        def spy_export(fmt, out_dir="data/exports"):
            spy_store.export_calls.append((fmt, out_dir))
            return f"{out_dir}/meals-20260709-120000.{fmt}"

        spy_store.export = spy_export

        handlers = meals.handlers(spy_store)
        result = handlers["export_meals"]({"format": "json"})

        assert "Exported" in result or "exported" in result.lower()
        assert "json" in result.lower()
        assert len(spy_store.export_calls) == 1
        assert spy_store.export_calls[0][0] == "json"

    def test_export_meals_handler_default_format(self, meal_store):
        """Test export_meals defaults to CSV when format is omitted."""
        spy_store = SimpleNamespace()
        spy_store.export_calls = []

        def spy_export(fmt, out_dir="data/exports"):
            spy_store.export_calls.append((fmt, out_dir))
            return f"{out_dir}/meals-20260709-120000.{fmt}"

        spy_store.export = spy_export

        handlers = meals.handlers(spy_store)
        result = handlers["export_meals"]({})

        assert len(spy_store.export_calls) == 1
        # Default should be csv
        assert spy_store.export_calls[0][0] == "csv"

    def test_export_meals_handler_invalid_format(self, meal_store):
        """Test export_meals with invalid format returns the store's ValueError as an
        Error string. Validation lives in ONE place (MealStore.export) — the handler
        must delegate, so the spy MUST be called."""
        spy_store = SimpleNamespace()
        spy_store.export_calls = []

        def spy_export(fmt, out_dir="data/exports"):
            spy_store.export_calls.append(fmt)
            raise ValueError(f"invalid fmt {fmt!r}; must be one of ['csv', 'json']")

        spy_store.export = spy_export

        handlers = meals.handlers(spy_store)
        result = handlers["export_meals"]({"format": "xml"})

        assert result.startswith("Error")
        assert "invalid fmt" in result
        # Single validation path: handler delegated to the store instead of pre-checking
        assert spy_store.export_calls == ["xml"]

    def test_log_and_summary_round_trip(self, meal_handlers, meal_store):
        """Test log→summary round trip shows totals and real DB ids."""
        # Log meals
        id1 = meal_handlers["log_meal"]({
            "description": "Morning coffee with milk",
            "calories": 80,
            "protein_g": 3.0,
            "carbs_g": 6.0,
            "fat_g": 4.0,
            "confidence": "medium",
            "quantity": "1 cup",
            "assumptions": "Standard milk portion"
        })
        # Extract ID from the response string
        assert "Meal logged" in id1 or "logged" in id1.lower()

        id2 = meal_handlers["log_meal"]({
            "description": "Lunch: grilled cheese",
            "calories": 420,
            "protein_g": 18.0,
            "carbs_g": 35.0,
            "fat_g": 20.0,
            "confidence": "high",
            "quantity": "2 slices",
            "assumptions": "Weighed bread and cheese"
        })

        summary = meal_handlers["meal_summary"]({})

        # Verify exact totals line per spec template (spaces between number and unit)
        assert summary.splitlines()[0] == (
            "Today: 2 meals, 500 kcal, 21.0 g protein, 41.0 g carbs, 24.0 g fat"
        )

        # Verify descriptions appear
        assert "coffee" in summary.lower()
        assert "grilled cheese" in summary.lower()

    def test_log_meal_handler_no_assumptions(self, meal_handlers):
        """Test log_meal handler without optional assumptions field."""
        result = meal_handlers["log_meal"]({
            "description": "Banana",
            "calories": 105,
            "protein_g": 1.3,
            "carbs_g": 27.0,
            "fat_g": 0.3,
            "confidence": "high"
        })
        assert "Meal logged" in result or "logged" in result.lower()
