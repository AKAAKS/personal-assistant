"""Meals: durable meal log with daily summary and CSV/JSON export."""

import csv
import json
import sqlite3
from datetime import datetime
from pathlib import Path

_VALID_FORMATS = {"csv", "json"}
_EXPORT_COLUMNS = [
    "date", "description", "quantity", "calories",
    "protein_g", "carbs_g", "fat_g", "confidence", "assumptions",
]


class MealStore:
    """Stores logged meals and serves add/today_summary/logged_today/export operations."""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def add(
        self,
        description: str,
        quantity: str,
        calories: int,
        protein_g: float,
        carbs_g: float,
        fat_g: float,
        confidence: str,
        assumptions: str,
    ) -> int:
        """Log a meal.

        Args:
            description: Free-text description of what was eaten.
            quantity: Free-text quantity/serving size.
            calories: Estimated calories.
            protein_g: Estimated protein in grams.
            carbs_g: Estimated carbs in grams.
            fat_g: Estimated fat in grams.
            confidence: Estimate confidence, e.g. 'low'/'medium'/'high'.
            assumptions: Free-text notes on assumptions made in the estimate.

        Returns:
            int: new meal id
        """
        ts = datetime.now().isoformat(timespec="seconds")
        cur = self._conn.execute(
            "INSERT INTO meals (ts, description, quantity, calories, protein_g, carbs_g, fat_g, "
            "confidence, assumptions) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ts, description, quantity, calories, protein_g, carbs_g, fat_g, confidence, assumptions),
        )
        self._conn.commit()
        return cur.lastrowid

    def today_summary(self, now: datetime | None = None) -> dict:
        """Summarize meals logged on the given day (default: today).

        Args:
            now: Reference datetime; only its date is used. Defaults to datetime.now().

        Returns:
            dict: {"meals": [rows], "calories": int, "protein_g": float,
                   "carbs_g": float, "fat_g": float}
        """
        date_prefix = (now or datetime.now()).date().isoformat()
        rows = self._conn.execute(
            "SELECT * FROM meals WHERE ts LIKE ? ORDER BY ts", (date_prefix + "%",)
        ).fetchall()
        return {
            "meals": rows,
            "calories": sum(r["calories"] for r in rows),
            "protein_g": sum(r["protein_g"] for r in rows),
            "carbs_g": sum(r["carbs_g"] for r in rows),
            "fat_g": sum(r["fat_g"] for r in rows),
        }

    def logged_today(self, now: datetime | None = None) -> bool:
        """Whether any meal has been logged on the given day (default: today).

        Args:
            now: Reference datetime; only its date is used. Defaults to datetime.now().

        Returns:
            bool: True if at least one meal was logged that day.
        """
        return len(self.today_summary(now=now)["meals"]) > 0

    def export(self, fmt: str, out_dir: str = "data/exports") -> str:
        """Export all logged meals to a CSV or JSON file.

        Args:
            fmt: 'csv' or 'json'.
            out_dir: Directory to write the export file into (created if missing).

        Returns:
            str: path to the written file.

        Raises:
            ValueError: if fmt is not 'csv' or 'json'.
        """
        if fmt not in _VALID_FORMATS:
            raise ValueError(f"invalid fmt {fmt!r}; must be one of {sorted(_VALID_FORMATS)}")

        rows = self._conn.execute("SELECT * FROM meals ORDER BY ts").fetchall()
        records = [
            {
                "date": r["ts"],
                "description": r["description"],
                "quantity": r["quantity"],
                "calories": r["calories"],
                "protein_g": r["protein_g"],
                "carbs_g": r["carbs_g"],
                "fat_g": r["fat_g"],
                "confidence": r["confidence"],
                "assumptions": r["assumptions"],
            }
            for r in rows
        ]

        Path(out_dir).mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = str(Path(out_dir) / f"meals-{stamp}.{fmt}")

        if fmt == "csv":
            with open(path, "w", encoding="utf-8", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=_EXPORT_COLUMNS)
                writer.writeheader()
                writer.writerows(records)
        else:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(records, f, indent=2)

        return path
