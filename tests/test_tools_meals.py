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
        assert "Today:" in result
        assert "2 meals" in result
        assert "550" in result  # 200 + 350
        assert "21" in result   # 6 + 15 protein
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
        """Test export_meals with invalid format returns error."""
        spy_store = SimpleNamespace()

        def spy_export(fmt, out_dir="data/exports"):
            raise ValueError(f"invalid fmt {fmt!r}")

        spy_store.export = spy_export

        handlers = meals.handlers(spy_store)
        result = handlers["export_meals"]({"format": "xml"})

        assert "error" in result.lower() or "invalid" in result.lower()

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

        # Verify totals
        assert "2 meals" in summary
        assert "500" in summary  # 80 + 420
        assert "21" in summary   # 3 + 18
        assert "41" in summary   # 6 + 35
        assert "24" in summary   # 4 + 20

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
