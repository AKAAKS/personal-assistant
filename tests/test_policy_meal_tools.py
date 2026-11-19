from security.policy import DEFAULT_PERMISSION, TOOL_PERMISSIONS


def test_day_status_is_explicitly_read_only():
    assert TOOL_PERMISSIONS["day_status"] == "read_only"


def test_log_estimated_meal_is_explicitly_write():
    assert TOOL_PERMISSIONS["log_estimated_meal"] == "write"


def test_log_meal_unchanged():
    assert TOOL_PERMISSIONS["log_meal"] == "write"
    assert DEFAULT_PERMISSION == "write"


# --- the invariant that makes the three assertions above unnecessary ---------
#
# TOOL_PERMISSIONS is keyed by tool NAME and lives in a different file from the
# tool it describes, and `guard.check` falls back to DEFAULT_PERMISSION when a
# name is absent. So a new tool-pack gets a tier by OMISSION rather than by
# decision, and nothing fails. Ten tools had reached production that way:
# lookup_food, pantry_add/list/set_quantity/remove, check_dish, suggest_meals,
# plan_prep, cook_meal and list_planned_meals -- the three commits that wired
# them (9572e26, 46faebf, 8f6bbae) never touched security/policy.py.
#
# This test does not care what tier a tool gets, only that a human chose one.

import importlib
import pkgutil

import jarvisd.tools


def _declared_tool_names():
    """Every tool name declared by any jarvisd.tools submodule."""
    names = set()
    for info in pkgutil.iter_modules(jarvisd.tools.__path__):
        mod = importlib.import_module(f"jarvisd.tools.{info.name}")
        for spec in getattr(mod, "TOOLS", []):
            names.add(spec["name"])
    return names


def test_every_declared_tool_has_an_explicit_permission_tier():
    declared = _declared_tool_names()
    assert declared, "found no tool specs -- the collector is broken, not the policy"
    missing = sorted(declared - set(TOOL_PERMISSIONS))
    assert not missing, (
        "these tools would silently inherit DEFAULT_PERMISSION="
        f"{DEFAULT_PERMISSION!r} instead of a chosen tier: {missing}"
    )


def test_no_permission_entry_names_a_tool_that_no_longer_exists():
    """The other direction: a renamed tool leaves a dead entry behind, and the
    renamed one then silently defaults."""
    stale = sorted(set(TOOL_PERMISSIONS) - _declared_tool_names())
    assert not stale, f"TOOL_PERMISSIONS entries with no matching tool: {stale}"


def test_every_tier_is_one_of_the_four_documented_values():
    allowed = {"read_only", "write", "destructive", "forbidden"}
    bad = {n: t for n, t in TOOL_PERMISSIONS.items() if t not in allowed}
    assert not bad, f"unrecognised tier(s), which guard.check treats as ALLOW: {bad}"
