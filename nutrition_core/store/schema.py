"""Shared SQLite schema for nutrition persistence (owned by nutrition_core).

Only exports SQL text; opening a DB and running migrations is the host's job
(JARVIS runs these as migration 7). Additive only: never drops or rewrites data.
"""

NEW_TABLES_SQL = """
CREATE TABLE goals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kcal INTEGER NOT NULL,
  protein_g REAL NOT NULL,
  carbs_g REAL NOT NULL,
  fat_g REAL NOT NULL,
  effective_from TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE meal_ingredients (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  meal_id INTEGER NOT NULL REFERENCES meals(id),
  name TEXT NOT NULL,
  grams REAL NOT NULL,
  per100_kcal REAL NOT NULL,
  per100_protein_g REAL NOT NULL,
  per100_carbs_g REAL NOT NULL,
  per100_fat_g REAL NOT NULL,
  source TEXT NOT NULL,
  kcal REAL NOT NULL,
  protein_g REAL NOT NULL,
  carbs_g REAL NOT NULL,
  fat_g REAL NOT NULL
);
CREATE TABLE pantry (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  source TEXT,
  external_id TEXT,
  grams_available REAL NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE disliked_foods (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  pattern TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE food_cache (
  source TEXT NOT NULL,
  external_id TEXT NOT NULL,
  name TEXT NOT NULL,
  per100_kcal REAL NOT NULL,
  per100_protein_g REAL NOT NULL,
  per100_carbs_g REAL NOT NULL,
  per100_fat_g REAL NOT NULL,
  fetched_at TEXT NOT NULL,
  PRIMARY KEY (source, external_id)
);
CREATE TABLE settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
ALTER TABLE meals ADD COLUMN status TEXT NOT NULL DEFAULT 'estimated';
ALTER TABLE meals ADD COLUMN name TEXT;
"""

# Idempotent: seeds one active goal from the legacy single-row targets table,
# only when it exists and goals is still empty.
SEED_GOALS_FROM_TARGETS_SQL = """
INSERT INTO goals (kcal, protein_g, carbs_g, fat_g, effective_from, active)
SELECT calories, protein_g, carbs_g, fat_g, datetime('now'), 1
FROM nutrition_targets
WHERE id = 1 AND NOT EXISTS (SELECT 1 FROM goals);
"""
