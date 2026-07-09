"""Tests for MealStore — meal logging, daily summary, and export."""

import csv
import json
from datetime import datetime

import pytest

from jarvisd.db import connect
from jarvisd.meal_store import MealStore


@pytest.fixture
def conn():
    """In-memory SQLite connection with schema."""
    c = connect(":memory:")
    yield c
    c.close()


@pytest.fixture
def store(conn):
    """MealStore instance."""
    return MealStore(conn)


class TestMealStoreAdd:
    def test_add_returns_id_and_stores_row(self, store, conn):
        mid = store.add(
            description="two eggs and toast",
            quantity="1 serving",
            calories=350,
            protein_g=18.5,
            carbs_g=30.0,
            fat_g=15.0,
            confidence="medium",
            assumptions="butter assumed",
        )
        assert isinstance(mid, int)
        row = conn.execute("SELECT * FROM meals WHERE id=?", (mid,)).fetchone()
        assert row["description"] == "two eggs and toast"
        assert row["quantity"] == "1 serving"
        assert row["calories"] == 350
        assert row["protein_g"] == 18.5
        assert row["carbs_g"] == 30.0
        assert row["fat_g"] == 15.0
        assert row["confidence"] == "medium"
        assert row["assumptions"] == "butter assumed"
        assert row["ts"]  # stamped


class TestMealStoreTodaySummary:
    def test_today_summary_totals_from_added_meals(self, store):
        store.add("eggs", "1", 300, 20.0, 2.0, 20.0, "high", "")
        store.add("toast", "2 slices", 200, 6.0, 30.0, 5.0, "high", "")
        summary = store.today_summary()
        assert len(summary["meals"]) == 2
        assert summary["calories"] == 500
        assert summary["protein_g"] == 26.0
        assert summary["carbs_g"] == 32.0
        assert summary["fat_g"] == 25.0

    def test_today_summary_excludes_yesterdays_meal(self, store, conn):
        yesterday_ts = "2026-07-08T08:00:00"
        conn.execute(
            "INSERT INTO meals (ts, description, quantity, calories, protein_g, carbs_g, fat_g, "
            "confidence, assumptions) VALUES (?, 'oatmeal', '1 bowl', 250, 8.0, 40.0, 5.0, 'high', '')",
            (yesterday_ts,),
        )
        conn.commit()
        now = datetime(2026, 7, 9, 12, 0, 0)
        summary = store.today_summary(now=now)
        assert summary["meals"] == []
        assert summary["calories"] == 0
        assert summary["protein_g"] == 0
        assert summary["carbs_g"] == 0
        assert summary["fat_g"] == 0

    def test_today_summary_uses_now_param_date(self, store, conn):
        conn.execute(
            "INSERT INTO meals (ts, description, quantity, calories, protein_g, carbs_g, fat_g, "
            "confidence, assumptions) VALUES ('2026-07-09T08:00:00', 'oatmeal', '1 bowl', 250, "
            "8.0, 40.0, 5.0, 'high', '')"
        )
        conn.commit()
        now = datetime(2026, 7, 9, 20, 0, 0)
        summary = store.today_summary(now=now)
        assert len(summary["meals"]) == 1
        assert summary["calories"] == 250


class TestMealStoreLoggedToday:
    def test_logged_today_false_when_empty(self, store):
        assert store.logged_today() is False

    def test_logged_today_true_after_add(self, store):
        store.add("eggs", "1", 300, 20.0, 2.0, 20.0, "high", "")
        assert store.logged_today() is True

    def test_logged_today_false_for_yesterday_only(self, store, conn):
        conn.execute(
            "INSERT INTO meals (ts, description, quantity, calories, protein_g, carbs_g, fat_g, "
            "confidence, assumptions) VALUES ('2026-07-08T08:00:00', 'oatmeal', '1 bowl', 250, "
            "8.0, 40.0, 5.0, 'high', '')"
        )
        conn.commit()
        now = datetime(2026, 7, 9, 12, 0, 0)
        assert store.logged_today(now=now) is False


class TestMealStoreExport:
    def test_export_csv_writes_header_and_row(self, store, tmp_path):
        store.add("eggs", "1", 300, 20.0, 2.0, 20.0, "high", "no assumptions")
        path = store.export("csv", out_dir=str(tmp_path))
        assert path.endswith(".csv")
        with open(path, "r", encoding="utf-8", newline="") as f:
            rows = list(csv.reader(f))
        assert rows[0] == [
            "date", "description", "quantity", "calories",
            "protein_g", "carbs_g", "fat_g", "confidence", "assumptions",
        ]
        assert rows[1][1] == "eggs"
        assert rows[1][3] == "300"

    def test_export_json_parses_and_has_expected_keys(self, store, tmp_path):
        store.add("eggs", "1", 300, 20.0, 2.0, 20.0, "high", "no assumptions")
        path = store.export("json", out_dir=str(tmp_path))
        assert path.endswith(".json")
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        assert len(data) == 1
        assert data[0]["description"] == "eggs"
        assert data[0]["calories"] == 300
        assert set(data[0].keys()) == {
            "date", "description", "quantity", "calories",
            "protein_g", "carbs_g", "fat_g", "confidence", "assumptions",
        }

    def test_export_bad_fmt_raises_value_error(self, store, tmp_path):
        with pytest.raises(ValueError):
            store.export("xml", out_dir=str(tmp_path))

    def test_export_returns_path_that_exists(self, store, tmp_path):
        store.add("eggs", "1", 300, 20.0, 2.0, 20.0, "high", "")
        path = store.export("csv", out_dir=str(tmp_path))
        import os
        assert os.path.exists(path)
