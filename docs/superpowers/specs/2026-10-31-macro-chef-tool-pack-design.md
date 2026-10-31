# Macro Chef — JARVIS Tool-Pack ("Full Loop") Design

**Date:** 2026-10-31
**Status:** Design — awaiting approval, then a plan (`superpowers:writing-plans`).
**Parent architecture:** `docs/superpowers/specs/2026-08-26-macro-chef-design.md` ("shared engine, two faces"). This document designs **Face 2 (JARVIS, in-process tool-pack)** and the **full morning-to-meal loop** on top of the already-merged `nutrition_core` engine (Phase 1) and persistence layer (Phase 2). SQLite only; no Turso/libSQL this phase.

---

## 1. Intent & Why

The user eats to a single daily macro/calorie target (set in MacroFactor, mirrored into JARVIS). Each day — typically in the kitchen, before the first of ~2 meals — they want JARVIS to hand them **ready-to-make dishes, with gram measurements, drawn from their own pantry, that fit the day's remaining macros**, with **zero-error numbers** (DB-sourced, deterministically computed — never LLM-invented). They do **not** want to negotiate with it; the only decision they want is *"this dish now, or a different one for my taste."* When nothing appeals they build their own. When they eat out, they want an honest **estimate** that's clearly marked as such, not a fake-precise number.

This phase turns the engine + persistence already built into something used daily, through conversation with JARVIS.

**Why now:** `nutrition_core` (models, engine, budget guard, USDA + Open Food Facts adapters) and the persistence layer (goals with history, verified-meal logging with per-100g snapshots, day totals / remaining) are merged to master. The plumbing exists; nothing yet drives it.

**Success looks like:** the user opens JARVIS in the morning, says roughly "what should I eat," and gets 2–3 curated dishes (one sweet, one savory, one generic) they can actually cook from what they have, each fitting their remaining budget exactly. They make one, it's logged, the pantry draws down, and the rest of the day rebalances — all without tweaking numbers by hand.

---

## 2. Goals / Non-Goals / Anti-Goals

### Goals
- **Morning suggestion loop:** given the day's remaining macros, pantry, dislikes, and an optional taste hint, propose a small curated set (≈ one sweet, one savory, one generic) of makeable dishes with gram measurements, each engine-computed and fit-checked against the remaining budget.
- **Floating daily budget, default ~2 meals:** no fixed meal slots. Next-meal suggestions sized to `remaining ÷ meals-likely-left`; logging a meal rebalances the rest.
- **Swap, don't tweak:** the user picks a suggested dish or asks for a different one (different taste profile). No "make it higher protein" haggling loop.
- **Create-your-own:** a fallback to build a dish from named ingredients + grams, logged with the same zero-error math.
- **Live pantry inventory (grams):** seeded once (barcode number or product name → Open Food Facts/USDA lookup → per-100g data; user states grams remaining), topped up as they shop, and **automatically depleted** by the exact grams of every dish they make.
- **Meal-prep mode:** the user can lock specific dishes for the coming day(s); the system sets **ingredient reminders and prep reminders a day ahead**, riding on JARVIS's existing reminder system.
- **Eating-out / estimated logging:** a free-text "I ate X" path that **decomposes** the description into DB-sourced component ingredients with *estimated grams*, computes deterministically, and logs as **`estimated`** with a confidence flag — distinct from measured **`verified`** meals.
- **Daily status:** an at-a-glance view of consumed vs. remaining macros, honestly splitting *verified* from *estimated*.
- **Accuracy preserved end-to-end:** every stored/returned number flows through `nutrition_core` (grams × per-100g, deterministic). The LLM proposes dishes, ingredients, and portion *estimates*; it never emits a final macro number.

### Non-Goals (this phase)
- **No Turso/libSQL.** SQLite (`data/jarvis.db`) only. (Windows embedded-replica wheel doesn't build on this setup; deferred to its own phase.)
- **No PWA build.** This phase is the JARVIS conversational tool-pack. (The PWA is a later face — see Standing Quality Bar.)
- **No camera barcode scanning in JARVIS.** On the PC, pantry items are added by *spoken/typed barcode number or product name*. Camera scan belongs to the PWA. (The OFF adapter already has barcode lookup; nothing is lost.)
- **No Nutritionix yet.** Named-chain restaurant data is a later upgrade; until then, eating-out is handled by the estimated path.
- **No workout- or body-scan-driven macro math.** One daily target drives everything; measurements, body scan, and workout plan are *informational profile data* only.

### Anti-Goals (would make this a failure even if it "works")
- The LLM inventing or guessing a calorie/macro total directly, or any displayed number not traceable to `grams × DB per-100g`.
- A tweak/haggle loop creeping in (the user explicitly does not want to negotiate).
- Estimated (eating-out) entries being silently mixed in as if they were measured — the honesty split is a requirement, not a nicety.
- Rigid fixed meal slots that fight the day-to-day float.
- Pantry drifting out of sync with reality (a made dish not drawing it down).

---

## 3. Constraints

- **Language/stack:** Python 3.11, in JARVIS (`jarvisd`), reusing the merged `nutrition_core` package. Tools follow JARVIS's existing registry pattern (`jarvisd/tools/__init__.py`: each module exports a `TOOLS` list + `handlers(deps)`); persistence uses the Phase-2 repos (`nutrition_core/store/`). Config via `.env` (`python-dotenv`).
- **Single source of truth:** JARVIS's SQLite DB. Tables from Phase 2 already exist: `goals`, `meals` (+ `meal_ingredients`, `status`, `name`), `pantry`, `disliked_foods`, `food_cache`, `settings`.
- **Live food lookup requires network + a credential:** USDA FoodData Central needs a **free API key** (`USDA_API_KEY` in `.env`); Open Food Facts needs none. This phase wires the **first live HTTP transport** — which triggers the gated hardening below.
- **Gated hardening (now due):** both adapters (USDA, OFF) must be hardened against null/non-dict payloads and non-numeric values **before** the live transport ships — carried over from Phase 1's parked ruling. A malformed live response must drop the food, never crash or invent a number.
- **Zero-error rule (inherited core principle):** code does all arithmetic from DB per-100g values; the LLM never produces a final number. Estimation is confined to *portion grams* and is always flagged.
- **Reminders:** reuse JARVIS's existing `reminders` table / scheduling — do not build a parallel reminder system.
- **Attribution:** commits end `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.

---

## 4. The Design (decided)

### 4.1 Meal model — floating daily budget
- The day has **one target** = the single active `goals` row (kcal/protein/carbs/fat).
- Default **~2 meals**, floating; typically one pre-gym, one between gym and bed. The user may eat 1–3+; nothing is slotted to a clock.
- When asked for a suggestion, the next meal's sub-target = `remaining_today ÷ meals-likely-left` (default assumes 2/day; *my call:* a simple, overridable expected-meal count, adjustable by the user saying e.g. "this is my last meal"). After a meal is logged, `remaining_today` shrinks and the next suggestion re-sizes automatically.
- **Swap rebalances:** rejecting/replacing a suggestion just re-queries against the same remaining budget; it never forces a slot.

### 4.2 Suggestions — curated, pantry-constrained, fit-checked
- On request, produce a small set — **≈ one sweet, one savory, one generic** (*the user's framing*) — of makeable dishes.
- Pipeline (preserves zero-error): the **LLM proposes** candidate dishes + their component ingredients + portion grams, constrained by *pantry contents, remaining sub-target, dislikes, and an optional daily taste/diet hint*. Each ingredient is **resolved to per-100g via the food DB** (pantry item → its stored snapshot; otherwise USDA/OFF, cached in `food_cache`). The **engine computes** each dish's macros; the **budget guard** checks fit against the sub-target; dishes that don't fit (or need ingredients not in pantry / insufficient grams) are adjusted, excluded, or flagged for an ingredient reminder.
- Each presented dish = **name + ingredient list with grams + brief method** (*my call:* enough to cook from, not a full recipe essay).
- Pantry-first: suggestions draw from what the user has. Missing/short ingredients are what surface as **ingredient reminders** (especially in prep mode), not silent substitutions.

### 4.3 Create-your-own
- If nothing appeals, the user names a dish and its ingredients + grams; it's resolved to DB per-100g and logged **verified**, depleting pantry like any made dish.

### 4.4 Pantry — live gram inventory
- **Seed once + top up:** add a product by **barcode number or product name** (→ OFF/USDA lookup → per-100g snapshot stored), then state **grams remaining**. Half-used items are entered with their real remaining grams.
- **Automatic depletion:** making a dish (suggested or create-your-own) subtracts each ingredient's exact grams from the matching pantry item. The pantry always reflects reality when the user measures.
- **Camera scan deferred** to the PWA; JARVIS uses barcode number / name.

### 4.5 Two modes
- **Day-to-day (default):** next-meal suggestions on demand, as above.
- **Meal-prep:** the user says "I'm prepping for tomorrow / the next N days"; the system locks specific dishes for those days (status `planned`), computes the shopping delta vs. pantry, and sets **ingredient + prep reminders a day ahead** via JARVIS's reminder system.

### 4.6 Eating out / estimated logging
- Free-text "I ate X": the **LLM decomposes** the description into component ingredients with **estimated grams**; each component resolved to DB per-100g; the **engine computes** the total. Logged as **`estimated`** with a **confidence flag** (`LOW`/`MEDIUM`), breakdown shown so the user can nudge a portion.
- The *only* estimate is portion grams — per-gram macros stay DB-sourced. Named chains become near-exact once Nutritionix lands (later).

### 4.7 Status
- A daily view: consumed vs. remaining per macro, with a clear **verified vs. estimated** split (so the user sees how much of the day is solid). Reuses `day_totals` / `remaining_today`.

### 4.8 Profile (informational)
- Store measurements, latest body scan, and workout plan (in `settings`/a profile store) for reference and future use; **does not drive** the daily target this phase.

### 4.9 Tool surface (indicative — final shape is the plan's job)
`lookup_food`, `pantry_add` / `pantry_set_quantity` / `pantry_list` / `pantry_remove`, `suggest_meal` (+ swap), `plan_prep`, `log_meal` (upgraded: verified / create-your-own / estimated paths; depletes pantry), `day_status`, `set_profile`. `set_nutrition_targets` already exists (goals-backed, Phase 2). The LLM-estimated `log_meal` ("not a food database") is **replaced** by the DB-sourced paths.

---

## 5. Standing Quality Bar (PWA, later)
When the PWA face is built, its **logging and daily-status experience must meet a top-tier health-app bar (MacroFactor as the reference)** — sophisticated, professional, not a hobby UI. Recorded here so the later phase treats it as a requirement, not throwaway.

---

## 6. Prerequisites
- **`USDA_API_KEY`** in `.env` (free, instant from FoodData Central). Open Food Facts needs none. Without the USDA key, lookup falls back to OFF only (branded foods) — the plan should degrade gracefully, not hard-fail.

---

## 7. What's Left to the Builder (the plan)
- Exact tool signatures, JSON schemas, and registry wiring.
- The live HTTP `Transport` implementation (httpx/requests) + adapter hardening + `food_cache` caching policy (TTL, key).
- Pantry↔ingredient matching strategy for depletion (by food id vs. name) and handling of partial/again-short quantities.
- The suggestion prompt/loop contract: how the LLM is constrained to propose-then-let-code-compute, how many candidates, how swap re-queries, how "doesn't fit" is resolved.
- Confidence/omission surfacing at the tool layer (ties to Phase-1's parked face-layer flagging).
- Reminder payload/shape for prep + ingredients on JARVIS's scheduler.
- Test strategy: fixtures for USDA/OFF (no live network in tests), hand-checked arithmetic cases, pantry-depletion round-trips, estimated-vs-verified separation.

---

## 8. Builder-check resolutions (my calls — all overridable)

A fresh reviewer read this as a builder and raised 8 points. None required the user; each is resolved here within their stated intent.

1. **Suggestion architecture — no nested LLM call.** JARVIS's outer conversation (already a Claude tool-use loop) proposes candidate dishes + ingredients + grams. A deterministic **`check_dish`** tool resolves each ingredient to per-100g (pantry snapshot → else USDA/OFF → `food_cache`), computes via the engine, and returns macros + budget fit. **`suggest_meal`** returns the *steering context* (remaining sub-target, pantry, dislikes, optional taste hint) for the proposal — it does not itself call an LLM. Result: cheap, fast, and testable with fixtures (no LLM mock). **Unresolvable ingredient:** drop the dish from "make now"; in prep mode a missing/short ingredient becomes a reminder. **Immediate suggestions are pantry-makeable; prep mode may include to-buy items** (that's what ingredient reminders are for).
2. **Statuses + planned counting.** Canonical: `verified` (measured, eaten), `estimated` (portion-guessed, eaten), `planned` (future, not yet eaten). **`day_totals`/`remaining_today` count only consumed (`verified` + `estimated`); `planned` is excluded** — a locked tomorrow-meal never eats today's budget. Cooking a `planned` meal transitions it in place to `verified` and depletes pantry at that moment. (Reconciles the Phase-2 `log_verified_meal` default of `eaten` → `verified`.) Needs a small schema/behavior change (new migration + a `day_totals` status filter).
3. **Prep reservations — none.** No per-day reservation structure; the prep shopping delta = aggregate of all planned-span ingredients minus current pantry. Depletion happens only at actual cook time. Simple, and yields a correct aggregate shopping list.
4. **Pantry ↔ per-100g + manual items.** Pantry rows carry (or join to) a per-100g snapshot (exact matching strategy is the builder's, schema may change). A pantry item normally resolves to a DB food (barcode/name). A no-DB item (label-only or homemade) may be entered as **`MANUAL`** source **from its nutrition label** (user transcribes the panel — a real, sourced number, flagged MANUAL) or decomposed into component ingredients. Preserves zero-error: no invented numbers; MANUAL = label-sourced.
5. **Estimated path.** Reuses the meals repo with `status=estimated` + `confidence` LOW/MEDIUM; the pre-commit breakdown lets the user correct a portion — this is *accuracy-correction of an estimate*, explicitly **not** the forbidden suggestion-haggling. **Estimated meals never deplete pantry** (not cooked from stock). Likely a thin repo parameter/extension, not a new table.
6. **Taste categories.** `sweet` / `savory` / `generic` are **LLM-assigned labels** (taste is subjective; code can't reliably classify); "generic" = a neutral everyday option. **If fewer than three fit the budget, show fewer and offer create-your-own — never silently relax the budget.**
7. **Edge cases.** No active goal → it asks the user to set targets before suggesting (can't fit to nothing). Over budget / cornered → surfaced honestly via the existing budget guard (`ok`/`over`/`cornered`); over-budget dishes are flagged or withheld, never passed off as fitting. Fit tolerance reuses the engine's existing ±50 kcal default, configurable later.
8. **Meals-per-day.** A `settings` default (`meals_per_day`, default 2) plus an optional per-call override on `suggest_meal` ("this is my last meal"); conversational, resets naturally per day.

---

## Appendix: Decisions the user made (theirs) vs. calls I made (mine)

**Theirs:** single daily target (kcal/P/C/F), no day-to-day workout-driven change; ~2 floating meals (pre/post gym); no haggling — swap only (this dish vs. another taste); curated sweet/savory/generic set from pantry; create-your-own fallback; pantry by barcode/name + remaining grams, auto-depleted by made dishes, seeded once + topped up; meal-prep with ingredient + prep reminders a day ahead; eating-out handled as honest `estimated` (decompose → DB → estimated grams), verified-vs-estimated split, chains→Nutritionix later; PWA held to MacroFactor-grade polish; workout/body-scan informational.

**Mine (defaults, overridable):** camera barcode scan is PWA-only, JARVIS uses number/name; next-meal sub-target = remaining ÷ expected-meals (default 2); dish presentation = name + ingredients(grams) + brief method; dislikes standing, diet/taste an optional daily hint; reminders reuse JARVIS's system; live HTTP transport added here with the gated adapter hardening; `food_cache` caches lookups; estimated entries carry LOW/MEDIUM confidence; SQLite-only, no Turso.
