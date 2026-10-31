"""Goals repository: dated goal history with a single active row."""

from __future__ import annotations

from datetime import datetime, timezone

from nutrition_core.models import Macros
from nutrition_core.store.connection import DbConnection


def _to_macros(row) -> Macros:
    return Macros(row["kcal"], row["protein_g"], row["carbs_g"], row["fat_g"])


def set_goals(conn: DbConnection, goals: Macros, *, effective_from: str | None = None) -> int:
    """Deactivate all rows, insert a new active row, commit once; return its id."""
    when = effective_from or datetime.now(timezone.utc).isoformat()
    # Both statements run in one implicit transaction, committed together.
    conn.execute("UPDATE goals SET active=0 WHERE active=1")
    cur = conn.execute(
        "INSERT INTO goals (kcal, protein_g, carbs_g, fat_g, effective_from, active) "
        "VALUES (?, ?, ?, ?, ?, 1)",
        (int(goals.kcal), goals.protein_g, goals.carbs_g, goals.fat_g, when),
    )
    new_id = cur.lastrowid
    conn.commit()
    return new_id


def active_goals(conn: DbConnection) -> Macros | None:
    row = conn.execute(
        "SELECT kcal, protein_g, carbs_g, fat_g FROM goals WHERE active=1 ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return _to_macros(row) if row else None


def goals_history(conn: DbConnection) -> list[Macros]:
    rows = conn.execute(
        "SELECT kcal, protein_g, carbs_g, fat_g FROM goals ORDER BY id DESC"
    ).fetchall()
    return [_to_macros(r) for r in rows]
