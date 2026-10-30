# Macro Chef — Design Spec

**Date:** 2026-08-26 (revised 2026-10-29 for JARVIS integration)
**Status:** Approved for planning
**Author:** akspam156@gmail.com

## Purpose

A personal system that helps decide meals for the day so they fit the user's
calorie and macronutrient targets. The user says what they feel like eating — a
cuisine, a specific dish, or the ingredients they have — and the system returns
meals with precise, verifiable ingredient quantities and macros that fit the
remaining budget for the day.

The defining requirement is **numerical accuracy**: every calorie and macro
figure shown — per ingredient, per dish, and for the day — must be correct and
traceable to a trusted source, never invented by a language model.

This system is delivered as **a shared accuracy engine with two faces**: a
kitchen-phone web app (PWA) and the user's existing JARVIS voice/text assistant.

## Core Principles (non-negotiable)

1. **The LLM never emits a final macro number.** Every number shown is produced
   by a deterministic **Nutrition Engine** from `grams × per-100g database
   values`. The model handles judgment (which dish, which ingredients, roughly
   how much); code handles all arithmetic.
2. **One accuracy engine, built once.** The food lookups, the deterministic
   math, the budget guard, and the planning logic live in a single Python
   package (`nutrition_core`) with **no LLM and no chat inside it**. Both faces
   reuse it.
3. **One source of truth.** Targets, the daily log, pantry, dislikes, meal
   history, and the food cache live in a single logical database (Turso /
   libSQL). The two faces read and write the same data.

## Users & Context

- Single user, personal tool.
- Already logs food in MacroFactor (no public API), so targets are entered into
  this system manually and stored; they change only when the user says so. The
  system tracks the running daily total itself as meals are confirmed.
- Already runs **JARVIS** (`github.com/AKAAKS/personal-assistant`): a Python
  Claude tool-use assistant with voice, memory, reminders, a tiered cost-aware
  router, a single security-guard choke point, and an existing meal subsystem
  (`MealStore` + `log_meal` / `meal_summary` / `set_nutrition_targets` /
  `meal_trends` / `export_meals`). JARVIS's meal tools currently use
  **LLM-estimated** macros ("not a food database") — this project upgrades them
  to DB-sourced accuracy and adds suggestion/planning.
- Used on a phone (installed PWA) and by voice through JARVIS.

## Architecture Overview

```
                         ┌─────────────────────────┐
                         │   nutrition_core (Py)   │   ← the asset, built once
                         │  food adapters · engine │
                         │  budget guard · planning│
                         │  data access · schema   │
                         └───────────┬─────────────┘
                                     │ (in-process import)        (HTTP)
                 ┌───────────────────┴───────────┐        ┌──────────────────┐
                 │  Face 2: JARVIS tool-pack      │        │ Engine Service   │
                 │  suggest_meal / plan_day /     │        │ (FastAPI wrapper │
                 │  lookup_food / pantry_* ;      │        │  around core)    │
                 │  log_meal upgraded             │        └────────┬─────────┘
                 └───────────────┬────────────────┘                 │ (HTTP)
                                 │                          ┌────────┴─────────┐
                                 │                          │ Face 1: Macro    │
                                 │                          │ Chef PWA         │
                                 │                          │ (Next.js/Vercel, │
                                 │                          │ Claude API chat) │
                                 │                          └────────┬─────────┘
                    ┌────────────┴───────────────┐                  │
                    │ libSQL EMBEDDED REPLICA     │   sync (bg)      │
                    │ local file on PC (JARVIS)   │◄────────────────►│
                    └─────────────┬───────────────┘                 │
                                  │                                  │
                          ┌───────┴──────────────────────────┐      │
                          │          Turso (cloud)            │◄─────┘
                          │   single source of truth          │
                          └───────────────────────────────────┘
```

- **`nutrition_core`** (Python package, lives in / alongside `personal-assistant`)
  — the shared accuracy engine. No LLM, no chat. Pure and heavily TDD'd.
- **Face 2 — JARVIS** imports `nutrition_core` **in-process** as a tool-pack and
  reaches the DB through a **libSQL embedded replica** (local file, fast,
  offline-tolerant, syncs to Turso in the background).
- **Engine Service** — a thin FastAPI wrapper around `nutrition_core`, pointing
  at Turso, deployed to a small always-on host. It exists so the PWA can reach
  the same logic over HTTP from the phone anywhere.
- **Face 1 — Macro Chef PWA** (Next.js/Vercel) — the kitchen-phone chat. Its
  conversation uses the **Claude API** directly; all accuracy work is delegated
  to the Engine Service. It never does nutrition math in TypeScript.
- **Turso (libSQL)** — the single source of truth.

### Why this split

The conversational LLM layer is **per-face** (JARVIS uses its tiered router;
the PWA uses the Claude API), because chat is cheap to have twice and each face
already has its own. The **accuracy engine is shared**, because it is the hard,
must-be-correct, worth-testing part and must never be duplicated or diverge.

## `nutrition_core` — the shared engine

A Python package with these modules, none of which call an LLM:

- **Food adapters** — USDA FoodData Central (whole foods, primary) and Open Food
  Facts (branded/barcoded), both free. Each returns a normalized shape:
  `{ name, per100g: {kcal, protein, carbs, fat}, source, externalId,
  confidence }`. Nutritionix is a documented later upgrade.
- **Nutrition Engine** — pure functions: `grams × per-100g`, summation,
  practical rounding, delta-vs-target. The only component allowed to produce a
  macro number. Exhaustively unit-tested — this is where "zero error" is
  enforced.
- **Budget guard** — computes remaining macros; detects when a choice would
  corner the rest of the day; warns.
- **Planning logic** — given remaining macros + constraints (cuisine, dislikes,
  pantry) + candidate foods, produce a fitted build sheet. (The LLM proposes the
  dish and candidate ingredients; the planner fits and verifies the numbers.)
- **Data access** — reads/writes the single store through an abstract connection
  interface (so tests use fast local SQLite and production uses libSQL).
- **Schema + migrations** — `nutrition_core` is the single owner of the schema.

## Data model (single store, Turso / libSQL)

`nutrition_core` owns the schema and its migrations. JARVIS stops creating
tables itself. Existing JARVIS columns are preserved so old estimated logs keep
working.

- `goals` — kcal, protein_g, carbs_g, fat_g, `effective_from`, `active`. New row
  on change; history preserved. (Replaces JARVIS's single-row
  `nutrition_targets`; a migration seeds this from the existing row.)
- `meals` — id, ts, date, name/description, status (`estimated` | `verified` |
  `planned` | `eaten`), quantity, confidence, assumptions, stored computed
  totals. Keeps the original JARVIS columns so historical rows remain valid.
- `meal_ingredients` — meal_id, name, grams, **snapshot of per-100g macros at
  log time**, source, computed macros. Snapshotting keeps a logged meal's
  numbers stable even if the source DB later changes. (Only populated for
  `verified` meals.)
- `pantry` — name, source ref, grams_available, updated_at.
- `disliked_foods` — name/pattern, note.
- `food_cache` — source, external_id, name, per-100g macros, fetched_at. Avoids
  refetching and makes numbers reproducible.
- `settings` — miscellaneous preferences.

## Persistence: embedded replica + sync

- **Turso cloud** holds the authoritative copy and is what the Engine Service
  (and therefore the PWA) reads/writes.
- **JARVIS uses a libSQL embedded replica**: a local SQLite file on the PC that
  serves reads instantly and works offline, syncing to Turso in the background
  (configured sync URL, auth token, sync interval). This preserves JARVIS's
  instant, offline-tolerant local feel.
- **Consistency rule (single user):** the face that performs a write is
  authoritative immediately for that write; the other face refreshes on
  focus/open. Brief cross-face staleness (seconds, until sync) is acceptable.

## Faces

### Face 1 — Macro Chef PWA (Next.js / Vercel)

- Chat-first screen with a persistent "remaining today" header and a meal
  build-sheet display; installable as a PWA.
- Conversation uses the **Claude API** (default `claude-opus-5`) behind a thin
  interface.
- All accuracy operations (lookup, compute, fit, log, remaining, export) call
  the **Engine Service** over HTTP. No nutrition math in TypeScript.

### Face 2 — JARVIS tool-pack (Python)

- New tools registered through the existing `jarvisd/tools/__init__.py` seam:
  `suggest_meal`, `plan_day`, `lookup_food`, and (Phase 2) `pantry_*`.
- `log_meal` is upgraded: it can take an ingredient/gram build sheet and compute
  **DB-sourced verified** macros via `nutrition_core`; the old estimate path
  remains as a fallback for quick logging.
- Reuses JARVIS's router, memory, voice, cost guard, and security guard. Reaches
  the DB through the embedded replica — no network hop for voice.

### Engine Service (FastAPI)

- Thin wrapper around `nutrition_core`, pointing at Turso. Endpoints:
  `/lookup`, `/compute`, `/fit`, `/log`, `/remaining`, `/export`.
- Deployed to a small always-on host (Fly / Railway / Render, a few dollars/mo)
  so the phone reaches it anywhere. (Alternative: run on the PC behind a
  Cloudflare Tunnel for zero cost, accepting "PC must be on." Default: paid host.)

## Primary Flow (meal-by-meal)

Illustrated for the PWA; JARVIS runs the same core logic by voice.

1. Open app → header shows **remaining today** (active goal − eaten).
2. User states a cuisine, a dish, or available ingredients.
3. The face's LLM reads goals, remaining, pantry, dislikes → proposes a dish +
   candidate ingredients with rough grams → asks `nutrition_core` to look up
   per-100g macros and fit portions.
4. The **Nutrition Engine** recomputes all totals from grams × per-100g; applies
   practical rounding.
5. The **budget guard** checks fit; if over/under or cornering the day, portions
   are scaled and/or deltas handed back to the LLM to iterate, then re-verified.
6. The face shows the **build sheet**: each ingredient with grams, per-ingredient
   macros, and a source tag; meal totals with the honest rounding delta; "what's
   left today after this"; cooking steps only if the dish needs them.
7. User confirms **"eating this"** → logged as a `verified`/`eaten` meal, daily
   total updated, pantry depleted by grams used (Phase 2). Or **"tweak it"** →
   iterate.

### Whole-day variant

"Plan my whole day" runs the same machinery but plans all meals up front so the
full day's totals fit the goal.

## Error Handling

- **Food not found** in either DB → ask the user to clarify or pick the closest
  match; user may enter per-100g manually, flagged low-confidence.
- **Ambiguous match** → pick the best generic, show the source, allow correction.
- **Low-quality branded data** → tagged so the user can verify/override.
- **LLM states a number** → ignored by design; only Engine numbers render.
- **Food DB/API unavailable** → use `food_cache` if present; otherwise a clear
  "couldn't verify this ingredient" message rather than a guess.
- **Offline (JARVIS)** → embedded replica serves reads/writes locally; sync
  resumes when back online.
- **Pantry short** (Phase 2) → warn; suggest a smaller portion or restock.
- **Core rule:** never silently invent a macro. Every number is sourced or
  explicitly user-entered-and-flagged; the rounding delta is always shown.

## Testing Strategy

- **Nutrition Engine** — TDD; exhaustive pure-function unit tests (fractional
  grams, zero, rounding boundaries, summation). Where "zero error" is enforced.
- **Food adapters** — tests against recorded USDA / Open Food Facts responses.
- **Budget guard** — corner-detection tests.
- **Data access** — tests run against fast local SQLite via the abstract
  connection interface; a thin integration test exercises libSQL.
- **JARVIS migration** — the existing `test_meal_store.py`,
  `test_meal_targets.py`, `test_tools_meals.py` keep passing (adapted to the
  connection abstraction, not rewritten); the `nutrition_targets` → `goals`
  migration is tested with a seed row. Protects the current green suite.
- **JARVIS tool-pack** — tests prove new tools route through `nutrition_core`
  and never emit final numbers from the LLM.
- **Engine Service** — endpoint tests over a test DB.
- **End-to-end smoke** — "Thai dinner within remaining macros" through both a
  JARVIS tool call and an Engine Service request.

## Build Order (phasing)

1. **`nutrition_core`** — food adapters + Nutrition Engine + budget guard, pure
   and TDD, with the abstract connection interface. Face-agnostic; build first.
2. **Shared schema + migrate JARVIS `MealStore`** onto `nutrition_core` + libSQL
   (embedded replica for JARVIS), migrating `nutrition_targets` → `goals` and
   preserving existing meal rows. Keep the current test suite green.
3. **Face 2 — JARVIS tool-pack** (`suggest_meal`, `plan_day`, `lookup_food`;
   upgrade `log_meal`). Fastest path to daily use via voice; validates the core
   end-to-end reusing JARVIS's loop/memory/voice/guards.
4. **Engine Service (FastAPI)** + deploy to the always-on host.
5. **Face 1 — Macro Chef PWA** — the kitchen chat calling the Engine Service.
6. **Later:** gram-accurate pantry + depletion, whole-day planning,
   meal-history/favorites learning, Nutritionix upgrade, and the portion-solver
   optimization — added only if the iterate-with-the-model loop proves annoying.

## Repository Layout

- `nutrition_core`, the JARVIS tool-pack changes, and the Engine Service live in
  / alongside **`personal-assistant`** (Python).
- The **PWA** lives in **`macro-chef`** (this repo; Next.js/TypeScript).
- This spec is the overarching design for both.

## Out of Scope

- Reading targets directly from MacroFactor (no public API).
- Barcode-scanning UI (Open Food Facts supports it, but not built initially).
- Multi-user support / accounts.
- A conversational LLM *inside* `nutrition_core` — chat is per-face by design.
