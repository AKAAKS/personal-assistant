from jarvisd import db
from jarvisd.db import connect

NEW_COLS = {"per100_kcal", "per100_protein_g", "per100_carbs_g", "per100_fat_g", "confidence"}


class TestPantryMigration8:
    def test_fresh_db_reaches_version_9(self):
        conn = connect(":memory:")
        row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
        assert db.SCHEMA_VERSION == 9
        assert row["v"] == 9

    def test_pantry_has_snapshot_columns(self):
        conn = connect(":memory:")
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(pantry)")}
        assert NEW_COLS <= cols
        assert {"id", "name", "source", "external_id", "grams_available", "updated_at"} <= cols

    def test_existing_row_still_readable_with_null_snapshot(self):
        conn = connect(":memory:")
        conn.execute(
            "INSERT INTO pantry (name, source, external_id, grams_available, updated_at) "
            "VALUES ('rice', 'usda', '1', 500, '2026-01-01')"
        )
        row = conn.execute("SELECT * FROM pantry WHERE name = 'rice'").fetchone()
        assert row["grams_available"] == 500
        assert row["per100_kcal"] is None
        assert row["confidence"] is None
