"""Pantry repository: inventory CRUD + the depletion primitive."""

from __future__ import annotations

from nutrition_core.models import Macros
from nutrition_core.store.connection import DbConnection

_COLUMNS = (
    "id, name, grams_available, source, external_id, per100_kcal, "
    "per100_protein_g, per100_carbs_g, per100_fat_g, confidence"
)


def _row_to_dict(row) -> dict:
    return {key: row[key] for key in row.keys()}


def add_or_topup(
    conn: DbConnection,
    *,
    name: str,
    grams: float,
    per100g: Macros,
    source: str,
    external_id: str | None,
    confidence: str,
) -> int:
    """Add stock. Same (source, external_id) tops up; manual items always insert."""
    snapshot = (
        per100g.kcal,
        per100g.protein_g,
        per100g.carbs_g,
        per100g.fat_g,
    )
    if external_id is not None:
        row = conn.execute(
            "SELECT id FROM pantry WHERE source=? AND external_id=?",
            (source, external_id),
        ).fetchone()
        if row is not None:
            conn.execute(
                "UPDATE pantry SET grams_available = grams_available + ?, "
                "per100_kcal=?, per100_protein_g=?, per100_carbs_g=?, "
                "per100_fat_g=?, confidence=?, updated_at=datetime('now') "
                "WHERE id=?",
                (grams, *snapshot, confidence, row["id"]),
            )
            conn.commit()
            return row["id"]
    cur = conn.execute(
        "INSERT INTO pantry (name, source, external_id, grams_available, "
        "per100_kcal, per100_protein_g, per100_carbs_g, per100_fat_g, "
        "confidence, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))",
        (name, source, external_id, grams, *snapshot, confidence),
    )
    conn.commit()
    return cur.lastrowid


def list_items(conn: DbConnection) -> list[dict]:
    rows = conn.execute(
        f"SELECT {_COLUMNS} FROM pantry ORDER BY name COLLATE NOCASE, id"
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def set_quantity(conn: DbConnection, pantry_id: int, grams: float) -> None:
    conn.execute(
        "UPDATE pantry SET grams_available=?, updated_at=datetime('now') WHERE id=?",
        (grams, pantry_id),
    )
    conn.commit()


def adjust_quantity(conn: DbConnection, pantry_id: int, delta_grams: float) -> float:
    """Add delta (negative depletes), floor at 0, delete the row when empty.

    Returns the remaining grams (0.0 if the row was removed).
    """
    row = conn.execute(
        "SELECT grams_available FROM pantry WHERE id=?", (pantry_id,)
    ).fetchone()
    if row is None:
        return 0.0
    remaining = max(0.0, row["grams_available"] + delta_grams)
    if remaining == 0.0:
        conn.execute("DELETE FROM pantry WHERE id=?", (pantry_id,))
    else:
        conn.execute(
            "UPDATE pantry SET grams_available=?, updated_at=datetime('now') "
            "WHERE id=?",
            (remaining, pantry_id),
        )
    conn.commit()
    return remaining


def find_by_food(
    conn: DbConnection, *, source: str, external_id: str
) -> dict | None:
    row = conn.execute(
        f"SELECT {_COLUMNS} FROM pantry WHERE source=? AND external_id=?",
        (source, external_id),
    ).fetchone()
    return _row_to_dict(row) if row is not None else None


def remove(conn: DbConnection, pantry_id: int) -> None:
    conn.execute("DELETE FROM pantry WHERE id=?", (pantry_id,))
    conn.commit()
