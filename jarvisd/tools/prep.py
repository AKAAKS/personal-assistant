"""Meal-prep tools: plan_prep (lock dishes as `planned` meals + shopping delta).

The LLM never supplies macros. Every ingredient is resolved to a real snapshot
(reusing meal_log.resolve_ingredient) and the engine computes the totals.
Planning does NOT touch today's budget (planned meals are not consumed) and
NEVER depletes the pantry; it only reports what is missing.
"""

from datetime import date as _date
from datetime import datetime, timedelta

from jarvisd.reminder_store import ReminderStore
from jarvisd.tools.meal_log import resolve_ingredient
from nutrition_core.engine import compute_ingredient, compute_meal
from nutrition_core.store.meals import cook_planned_meal, list_planned, log_verified_meal

TOOLS = [
    {
        "name": "plan_prep",
        "description": (
            "Lock specific dishes for upcoming day(s) as PLANNED meals (they do not use "
            "today's budget and do not touch pantry stock) and get back a shopping list: "
            "what the plan needs minus what the pantry holds. Give 'dishes': each has "
            "'name', 'date' (YYYY-MM-DD) and 'ingredients' (each 'grams' plus exactly ONE "
            "of 'barcode', 'name' or 'pantry_id'). Macros are computed from real food data. "
            "A dish with an unresolvable ingredient is skipped and reported."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "dishes": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "date": {"type": "string", "description": "YYYY-MM-DD."},
                            "ingredients": {
                                "type": "array",
                                "minItems": 1,
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "grams": {"type": "number"},
                                        "barcode": {"type": "string"},
                                        "name": {"type": "string"},
                                        "pantry_id": {"type": "integer"},
                                    },
                                    "required": ["grams"],
                                },
                            },
                        },
                        "required": ["name", "date", "ingredients"],
                    },
                },
            },
            "required": ["dishes"],
        },
    },
    {
        "name": "cook_meal",
        "description": (
            "Mark a locked (planned) meal as actually COOKED: it becomes a logged, verified "
            "meal that counts toward today, and its ingredients are deducted from the pantry. "
            "Takes the meal's integer 'meal_id' (see list_planned_meals). Works once per meal."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"meal_id": {"type": "integer"}},
            "required": ["meal_id"],
        },
    },
    {
        "name": "list_planned_meals",
        "description": (
            "List upcoming locked (planned, not yet cooked) meals with id, name, date and "
            "macros, so one can be cooked with cook_meal. Optional 'from_date' (YYYY-MM-DD, "
            "default today)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"from_date": {"type": "string", "description": "YYYY-MM-DD."}},
        },
    },
]


def _g(x: float) -> str:
    return f"{x:g}"


def _key(r):
    if r.pantry_id is not None:
        return ("pid", r.pantry_id)
    if r.external_id:
        return ("food", r.source, r.external_id)
    return ("name", r.meal_ingredient.name.strip().lower())


def _shopping_delta(resolved_dishes, pantry_store) -> list[dict]:
    """Aggregate grams needed per food across all dishes minus pantry stock.

    `resolved_dishes` is a list of lists of ResolvedIngredient. Only items with
    a positive shortfall are returned: {"name","need_g","have_g","buy_g"}.
    """
    items = pantry_store.list_items()
    by_id = {row["id"]: row for row in items}
    need: dict[tuple, float] = {}
    rows: dict[tuple, dict | None] = {}
    label: dict[tuple, str] = {}
    for dish in resolved_dishes:
        for r in dish:
            # Normalize to the pantry row when one exists, so every identifier
            # form (pantry_id / barcode / name) of the same food shares a bucket.
            row = None
            if r.pantry_id is not None:
                row = by_id.get(r.pantry_id)
            elif r.external_id:
                row = pantry_store.find_by_food(r.source, r.external_id)
            if row is not None:
                k = ("pid", row["id"])
            elif r.external_id:
                k = ("food", r.source, r.external_id)
            else:
                k = ("name", r.meal_ingredient.name.strip().lower())
                row = next((i for i in items if i["name"].strip().lower() == k[1]), None)
                if row is not None:
                    k = ("pid", row["id"])
            need[k] = need.get(k, 0.0) + r.meal_ingredient.grams
            rows.setdefault(k, row)
            label.setdefault(k, r.meal_ingredient.name)

    out = []
    for k, n in need.items():
        row = rows[k]
        have = float(row["grams_available"]) if row else 0.0
        buy = max(0.0, n - have)
        if buy > 0:
            out.append({"name": row["name"] if row else label[k],
                        "need_g": n, "have_g": have, "buy_g": buy})
    return out


_HINT_TIME = "T18:00:00"


def _day_before(date_iso: str, *, now: datetime) -> str:
    """ISO due_ts for (date - 1 day) at 18:00; clamped to `now` if already past."""
    due = datetime.fromisoformat(
        (_date.fromisoformat(date_iso) - timedelta(days=1)).isoformat() + _HINT_TIME)
    if due <= now:
        return now.isoformat(timespec="seconds")
    return due.isoformat(timespec="seconds")


def _set_reminders(conn, locked, delta) -> list[str]:
    """Add a prep reminder per distinct date + a shopping one if delta. Returns descriptions."""
    store = ReminderStore(conn)
    now = datetime.now()
    by_date: dict[str, list[str]] = {}
    for name, day, _m in locked:
        by_date.setdefault(day, []).append(name)
    done = []
    for day in sorted(by_date):
        due = _day_before(day, now=now)
        store.add(f"Meal prep for {day}: {', '.join(by_date[day])}", due, recurrence="none")
        done.append(f"prep for {day} at {due}")
    if delta:
        due = _day_before(min(by_date), now=now)
        items = ", ".join(f"{it['name']} {_g(it['buy_g'])} g" for it in delta)
        store.add(f"Shopping for meal prep: {items}", due, recurrence="none")
        done.append(f"shopping at {due}")
    return done


def handlers(conn, food_service, pantry_store):
    def plan_prep(ti):
        try:
            if not isinstance(ti, dict):
                return "Error: invalid input."
            dishes = ti.get("dishes")
            if not isinstance(dishes, list) or not dishes:
                return "Error: 'dishes' must be a non-empty list."
            for d in dishes:
                if not isinstance(d, dict):
                    return "Error: each dish must be an object."
                if not isinstance(d.get("name"), str) or not d["name"].strip():
                    return "Error: each dish needs a 'name'."
                if not isinstance(d.get("date"), str) or not d["date"].strip():
                    return "Error: each dish needs a 'date' (YYYY-MM-DD)."
                if not isinstance(d.get("ingredients"), list) or not d["ingredients"]:
                    return "Error: each dish needs a non-empty 'ingredients' list."

            locked, skipped, resolved_locked = [], [], []
            for d in dishes:
                name, day = d["name"].strip(), d["date"].strip()
                try:
                    _date.fromisoformat(day)
                except ValueError:
                    skipped.append((name, f"date {day!r} is not YYYY-MM-DD."))
                    continue
                resolved, err = [], None
                for spec in d["ingredients"]:
                    r = resolve_ingredient(spec, food_service=food_service,
                                           pantry_store=pantry_store)
                    if isinstance(r, str):
                        err = r
                        break
                    resolved.append(r)
                if err is not None:
                    skipped.append((name, err))
                    continue
                total = compute_meal([
                    compute_ingredient(r.meal_ingredient.name, r.meal_ingredient.grams,
                                       r.meal_ingredient.per100g, r.meal_ingredient.source)
                    for r in resolved]).total
                # Planned only: never depletes the pantry.
                log_verified_meal(
                    conn, name=name, date=day,
                    ingredients=[r.meal_ingredient for r in resolved],
                    status="planned",
                    links=[(r.external_id, r.pantry_id) for r in resolved])
                locked.append((name, day, total))
                resolved_locked.append(resolved)

            lines = []
            if locked:
                lines.append("Locked (planned, not eaten; pantry unchanged):")
                for name, day, m in locked:
                    lines.append(f"- {name} on {day}: {m.kcal:.0f} kcal, {m.protein_g:.1f}g "
                                 f"protein, {m.carbs_g:.1f}g carbs, {m.fat_g:.1f}g fat")
            if skipped:
                lines.append("Skipped (NOT locked):")
                for name, why in skipped:
                    lines.append(f"- {name}: {why}")
            if locked:
                delta = _shopping_delta(resolved_locked, pantry_store)
                if delta:
                    lines.append("Shopping list:")
                    for it in delta:
                        lines.append(f"- {it['name']}: buy {_g(it['buy_g'])} g "
                                     f"(need {_g(it['need_g'])} g, have {_g(it['have_g'])} g)")
                else:
                    lines.append("Nothing to buy — your pantry covers this plan.")
                try:
                    done = _set_reminders(conn, locked, delta)
                    lines.append("Reminders set: " + "; ".join(done) + ".")
                except Exception as exc:
                    lines.append(f"Plan stored, but couldn't set reminders ({exc}).")
            return "\n".join(lines)
        except Exception as exc:
            return f"Error: couldn't plan the prep ({exc})"

    def _deplete_one(ing):
        """Best-effort deplete. Returns (remaining_or_None, note_or_None)."""
        grams = float(ing["grams"])
        pid = ing.get("pantry_id")
        if pid is not None:
            try:
                if any(r["id"] == pid for r in pantry_store.list_items()):
                    return pantry_store.deplete_by_id(pid, grams), None
            except Exception:
                pass  # fall through to the food link
        if ing.get("external_id"):
            rem = pantry_store.deplete(source=ing.get("source"),
                                       external_id=ing["external_id"], grams=grams)
            if rem is None:
                return None, f"couldn't deplete {ing['name']} (not in pantry)"
            return rem, None
        return None, f"couldn't match {ing['name']} to a pantry item"

    def cook_meal(ti):
        try:
            mid = ti.get("meal_id") if isinstance(ti, dict) else None
            if isinstance(mid, bool) or not isinstance(mid, int):
                return "Error: 'meal_id' must be an integer."
            res = cook_planned_meal(conn, mid)
            if not res.get("ok"):
                return ("That meal isn't a locked plan (maybe already cooked or doesn't "
                        "exist) — nothing was depleted.")
            depleted, misses = [], []
            for ing in res["ingredients"]:
                try:
                    rem, note = _deplete_one(ing)
                except Exception as exc:
                    rem, note = None, f"couldn't deplete {ing['name']} ({exc})"
                if note:
                    misses.append(note)
                else:
                    depleted.append(f"- {ing['name']}: -{_g(float(ing['grams']))} g "
                                    f"({_g(rem)} g left)")
            lines = [f"Cooked {res['name']}: it is now logged (verified) and counts toward "
                     f"{str(res['date'])[:10]}."]
            if depleted:
                lines.append("Pantry depleted:")
                lines += depleted
            if misses:
                lines.append("Not depleted:")
                lines += [f"- {m}" for m in misses]
            return "\n".join(lines)
        except Exception as exc:
            return f"Error: couldn't cook the meal ({exc})"

    def list_planned_meals(ti):
        try:
            frm = ti.get("from_date") if isinstance(ti, dict) else None
            if frm is None or frm == "":
                frm = _date.today().isoformat()
            else:
                try:
                    _date.fromisoformat(frm)
                except (ValueError, TypeError):
                    return f"Error: from_date {frm!r} is not YYYY-MM-DD."
            meals = list_planned(conn, from_date=frm)
            if not meals:
                return f"No planned meals from {frm}."
            lines = [f"Planned meals from {frm}:"]
            for m in meals:
                x = m["macros"]
                lines.append(f"- #{m['id']} {m['name']} on {str(m['date'])[:10]}: "
                             f"{x.kcal:.0f} kcal, {x.protein_g:.1f}P / {x.carbs_g:.1f}C / "
                             f"{x.fat_g:.1f}F")
            return "\n".join(lines)
        except Exception as exc:
            return f"Error: couldn't list planned meals ({exc})"

    return {"plan_prep": plan_prep, "cook_meal": cook_meal,
            "list_planned_meals": list_planned_meals}
