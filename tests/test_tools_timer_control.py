"""timer_control: resolving which timer, and each action."""

from datetime import datetime

import pytest

from jarvisd import db
from jarvisd.timer_store import TimerStore
from jarvisd.tools.reminders import handlers

NOW = datetime(2026, 11, 15, 10, 0, 0)


@pytest.fixture
def conn(tmp_path):
    c = db.connect(str(tmp_path / "t.db"))
    yield c
    c.close()


@pytest.fixture
def store(conn):
    return TimerStore(conn)


@pytest.fixture
def h(store):
    return handlers(None, store, clock=lambda: NOW)


def test_pause_and_resume_by_label(h, store):
    h["set_timer"]({"minutes": 10, "label": "tea"})
    assert "paused" in h["timer_control"]({"action": "pause", "label": "tea"}).lower()
    assert store.list_live(NOW)[0]["status"] == "paused"
    assert "resumed" in h["timer_control"]({"action": "resume", "label": "tea"}).lower()
    assert store.list_live(NOW)[0]["status"] == "running"


def test_label_matching_is_case_insensitive(h, store):
    h["set_timer"]({"minutes": 10, "label": "Tea"})
    h["timer_control"]({"action": "pause", "label": "tEA"})
    assert store.list_live(NOW)[0]["status"] == "paused"


def test_a_label_that_contradicts_the_timer_id_is_an_error_and_changes_nothing(h, store):
    # Replaces a test that pinned the opposite (timer_id silently beat a wrong
    # label): that silence is what let a false "it's now called trash" live on.
    tid = store.add(600, "tea", now=NOW)
    out = h["timer_control"]({"action": "pause", "timer_id": tid, "label": "ghost"})
    assert out.startswith("Error")
    assert "ghost" in out and "tea" in out and f"#{tid}" in out
    assert store.get(tid)["status"] == "running"


def test_a_matching_label_alongside_timer_id_is_accepted_case_insensitively(h, store):
    tid = store.add(600, "Tea", now=NOW)
    out = h["timer_control"]({"action": "pause", "timer_id": tid, "label": " tEA "})
    assert out == f"Paused Tea (#{tid})."
    assert store.get(tid)["status"] == "paused"


def test_a_label_on_an_unlabelled_timer_addressed_by_id_is_an_error(h, store):
    # The row's '' must not "match" a non-empty label.
    tid = store.add(600, "", now=NOW)
    before = store.get(tid)["due_ts"]
    out = h["timer_control"]({"action": "extend", "timer_id": tid, "minutes": 1, "label": "trash"})
    assert out.startswith("Error") and "trash" in out and "unnamed" in out
    assert store.get(tid)["due_ts"] == before


def test_a_non_string_label_is_an_error_not_silently_ignored(h, store):
    store.add(600, "tea", now=NOW)
    out = h["timer_control"]({"action": "pause", "label": 7})
    assert out.startswith("Error") and "label" in out
    assert store.list_live(NOW)[0]["status"] == "running"


def test_new_label_is_an_error_on_any_action_but_rename(h, store):
    tid = store.add(600, "tea", now=NOW)
    out = h["timer_control"]({"action": "pause", "timer_id": tid, "new_label": "x"})
    assert out.startswith("Error") and "rename" in out
    assert store.get(tid)["status"] == "running"


# -- rename -------------------------------------------------------------------


def test_rename_with_timer_id_and_label_treats_label_as_the_new_name(h, store):
    # Exactly the call shape the model emitted in the live test.
    tid = store.add(600, "", now=NOW)
    out = h["timer_control"]({"action": "rename", "timer_id": tid, "label": "trash"})
    assert out == f"Renamed unnamed timer (#{tid}) to 'trash'."
    assert store.get(tid)["label"] == "trash"
    assert [r["id"] for r in store.find_by_label("trash")] == [tid]


def test_rename_with_new_label_selects_by_timer_id(h, store):
    tid = store.add(600, "tea", now=NOW)
    store.add(300, "pasta", now=NOW)
    out = h["timer_control"]({"action": "rename", "timer_id": tid, "new_label": "green tea"})
    assert out == f"Renamed tea (#{tid}) to 'green tea'."
    assert store.get(tid)["label"] == "green tea"


def test_rename_selects_by_label_when_new_label_is_given(h, store):
    tid = store.add(600, "tea", now=NOW)
    other = store.add(300, "pasta", now=NOW)
    h["timer_control"]({"action": "rename", "label": "TEA", "new_label": "chai"})
    assert store.get(tid)["label"] == "chai"
    assert store.get(other)["label"] == "pasta"


def test_rename_with_only_label_is_ambiguous_and_asks_for_new_label(h, store):
    tid = store.add(600, "", now=NOW)
    out = h["timer_control"]({"action": "rename", "label": "trash"})
    assert out.startswith("Error") and "new_label" in out
    assert store.get(tid)["label"] == ""


def test_rename_with_no_name_at_all_is_an_error(h, store):
    tid = store.add(600, "tea", now=NOW)
    out = h["timer_control"]({"action": "rename", "timer_id": tid})
    assert out.startswith("Error") and "new_label" in out
    assert store.get(tid)["label"] == "tea"


@pytest.mark.parametrize("bad", ["", "   ", 5])
def test_rename_to_a_blank_or_non_string_name_is_an_error(h, store, bad):
    tid = store.add(600, "tea", now=NOW)
    out = h["timer_control"]({"action": "rename", "timer_id": tid, "new_label": bad})
    assert out.startswith("Error") and "new_label" in out
    assert store.get(tid)["label"] == "tea"


def test_rename_strips_whitespace_from_the_new_name(h, store):
    tid = store.add(600, "", now=NOW)
    h["timer_control"]({"action": "rename", "timer_id": tid, "new_label": "  trash  "})
    assert store.get(tid)["label"] == "trash"


def test_rename_with_a_single_live_timer_needs_no_selector(h, store):
    tid = store.add(600, "", now=NOW)
    out = h["timer_control"]({"action": "rename", "new_label": "trash"})
    assert out == f"Renamed unnamed timer (#{tid}) to 'trash'."


def test_rename_with_several_live_timers_and_no_selector_lists_them(h, store):
    a = store.add(600, "tea", now=NOW)
    store.add(300, "pasta", now=NOW)
    out = h["timer_control"]({"action": "rename", "new_label": "x"})
    assert out.startswith("Which timer?")
    assert store.get(a)["label"] == "tea"


def test_rename_with_a_selector_label_that_contradicts_timer_id_is_an_error(h, store):
    # new_label is the name, so label is a selector and must agree with timer_id.
    tid = store.add(600, "tea", now=NOW)
    out = h["timer_control"]({"action": "rename", "timer_id": tid, "label": "ghost", "new_label": "x"})
    assert out.startswith("Error") and "ghost" in out
    assert store.get(tid)["label"] == "tea"


def test_rename_works_on_a_ringing_timer(h, store):
    tid = store.add(60, "", now=NOW)
    store.mark_ringing(tid, NOW)
    h["timer_control"]({"action": "rename", "timer_id": tid, "new_label": "trash"})
    assert store.get(tid)["label"] == "trash"
    assert store.get(tid)["status"] == "ringing"


@pytest.mark.parametrize("finish", ["dismiss", "cancel"])
def test_rename_refuses_a_finished_timer(h, store, finish):
    # Policy: only live (running/paused/ringing) timers can be renamed, the same
    # set every other action can address. A finished timer's label is history.
    tid = store.add(600, "tea", now=NOW)
    getattr(store, finish)(tid)
    out = h["timer_control"]({"action": "rename", "timer_id": tid, "new_label": "x"})
    assert out == f"No live timer #{tid}."
    assert store.get(tid)["label"] == "tea"


# -- the live-test cascade ----------------------------------------------------


def test_cascade_extend_with_stray_label_then_dismiss_by_that_label(h, store):
    """Live test: extend {timer_id, label:'trash'} used to drop the label and say
    nothing, then dismiss {label:'trash'} found no timer while the alarm rang."""
    tid = store.add(600, "", now=NOW)
    due_before = store.get(tid)["due_ts"]
    extend = h["timer_control"]({"action": "extend", "timer_id": tid, "minutes": 1, "label": "trash"})
    # The user is not silently misled: the call is refused and says why ...
    assert extend.startswith("Error")
    assert "trash" in extend and "unnamed" in extend
    # ... and nothing changed (no extension applied under a false belief).
    assert store.get(tid)["due_ts"] == due_before
    assert store.get(tid)["label"] == ""
    # The model's recovery: rename, which now exists, then extend.
    renamed = h["timer_control"]({"action": "rename", "timer_id": tid, "label": "trash"})
    assert renamed == f"Renamed unnamed timer (#{tid}) to 'trash'."
    h["timer_control"]({"action": "extend", "timer_id": tid, "minutes": 1})
    # It rings; "stop the trash timer" now finds it and the alarm stops.
    store.mark_ringing(tid, NOW)
    out = h["timer_control"]({"action": "dismiss", "label": "trash"})
    assert out == f"Dismissed trash (#{tid})."
    assert store.get(tid)["status"] == "done"
    # A bare dismiss also works when exactly one timer is live.
    t2 = store.add(60, "", now=NOW)
    store.mark_ringing(t2, NOW)
    assert h["timer_control"]({"action": "dismiss"}) == f"Dismissed unnamed timer (#{t2})."


def test_extend_adds_minutes_and_says_the_new_time(h, store):
    h["set_timer"]({"minutes": 10, "label": "tea"})
    out = h["timer_control"]({"action": "extend", "label": "tea", "minutes": 5})
    assert "15 minutes left" in out
    assert store.list_live(NOW)[0]["due_ts"] == "2026-11-15T10:15:00"


def test_cancel_removes_the_timer(h, store):
    h["set_timer"]({"minutes": 10, "label": "tea"})
    assert "cancelled" in h["timer_control"]({"action": "cancel", "label": "tea"}).lower()
    assert store.list_live(NOW) == []


def test_dismiss_stops_a_ringing_timer(h, store):
    tid = store.add(60, "tea", now=NOW)
    store.mark_ringing(tid, NOW)
    assert "dismissed" in h["timer_control"]({"action": "dismiss", "label": "tea"}).lower()
    assert store.get(tid)["status"] == "done"


def test_snooze_a_ringing_timer_for_the_given_minutes(h, store):
    tid = store.add(60, "tea", now=NOW)
    store.mark_ringing(tid, NOW)
    out = h["timer_control"]({"action": "snooze", "label": "tea", "minutes": 9})
    assert "9 minutes" in out
    row = store.get(tid)
    assert row["status"] == "running" and row["due_ts"] == "2026-11-15T10:09:00"


def test_snooze_defaults_to_five_minutes(h, store):
    tid = store.add(60, "tea", now=NOW)
    store.mark_ringing(tid, NOW)
    h["timer_control"]({"action": "snooze", "label": "tea"})
    assert store.get(tid)["due_ts"] == "2026-11-15T10:05:00"


def test_snoozing_a_timer_that_is_not_ringing_reports_it_did_not_apply(h, store):
    tid = store.add(600, "tea", now=NOW)
    out = h["timer_control"]({"action": "snooze", "label": "tea", "minutes": 5})
    assert "couldn't snooze" in out.lower()
    assert store.get(tid)["due_ts"] == "2026-11-15T10:10:00"


def test_control_targets_the_only_timer_when_nothing_is_named(h, store):
    h["set_timer"]({"minutes": 10})
    assert "paused" in h["timer_control"]({"action": "pause"}).lower()
    assert store.list_live(NOW)[0]["status"] == "paused"


def test_control_asks_which_one_when_several_are_live_and_changes_nothing(h, store):
    h["set_timer"]({"minutes": 10, "label": "tea"})
    h["set_timer"]({"minutes": 20, "label": "pasta"})
    out = h["timer_control"]({"action": "pause"})
    assert "which timer" in out.lower()
    assert "tea" in out and "pasta" in out
    assert {r["status"] for r in store.list_live(NOW)} == {"running"}


def test_control_asks_which_one_when_a_label_matches_several(h, store):
    store.add(600, "tea", now=NOW)
    store.add(900, "tea", now=NOW)
    out = h["timer_control"]({"action": "pause", "label": "tea"})
    assert "which timer" in out.lower()
    assert {r["status"] for r in store.list_live(NOW)} == {"running"}


def test_dismiss_with_no_target_picks_the_one_ringing_timer_beside_running_ones(h, store):
    ringing = store.add(60, "tea", now=NOW)
    store.mark_ringing(ringing, NOW)
    other = store.add(900, "pasta", now=NOW)
    h["timer_control"]({"action": "dismiss"})
    assert store.get(ringing)["status"] == "done"
    assert store.get(other)["status"] == "running"


@pytest.mark.parametrize("action", ["pause", "cancel"])
def test_a_stray_zero_minutes_is_ignored_by_actions_that_do_not_use_it(h, store, action):
    tid = store.add(600, "tea", now=NOW)
    out = h["timer_control"]({"action": action, "label": "tea", "minutes": 0})
    assert "error" not in out.lower()
    assert store.get(tid)["status"] == {"pause": "paused", "cancel": "cancelled"}[action]


def test_null_minutes_label_and_timer_id_are_treated_as_absent(h, store):
    tid = store.add(60, "tea", now=NOW)
    store.mark_ringing(tid, NOW)
    out = h["timer_control"]({"action": "snooze", "minutes": None, "label": None, "timer_id": None})
    assert "error" not in out.lower()
    assert store.get(tid)["due_ts"] == "2026-11-15T10:05:00"


def test_extend_rejects_non_finite_minutes(h, store):
    store.add(600, "tea", now=NOW)
    assert "error" in h["timer_control"]({"action": "extend", "minutes": float("nan")}).lower()


def test_dismiss_with_no_target_and_two_ringing_timers_asks_and_changes_nothing(h, store):
    a = store.add(60, "tea", now=NOW)
    b = store.add(60, "eggs", now=NOW)
    store.mark_ringing(a, NOW)
    store.mark_ringing(b, NOW)
    out = h["timer_control"]({"action": "dismiss"})
    assert "which timer" in out.lower() and "tea" in out and "eggs" in out
    assert store.get(a)["status"] == "ringing" and store.get(b)["status"] == "ringing"


def test_dismiss_with_no_target_ends_a_lone_running_timer(h, store):
    tid = store.add(600, "tea", now=NOW)
    assert "dismissed" in h["timer_control"]({"action": "dismiss"}).lower()
    assert store.get(tid)["status"] == "done"


def test_control_on_an_unknown_label_does_not_raise(h):
    assert "no live timer" in h["timer_control"]({"action": "pause", "label": "ghost"}).lower()


def test_control_on_an_unknown_id_does_not_raise(h):
    assert "no live timer #99" in h["timer_control"]({"action": "pause", "timer_id": 99}).lower()


def test_control_with_no_timers_at_all_says_so(h):
    assert "no timers" in h["timer_control"]({"action": "pause"}).lower()


def test_a_finished_timer_cannot_be_controlled_by_id(h, store):
    tid = store.add(60, "tea", now=NOW)
    store.mark_ringing(tid, NOW)
    store.dismiss(tid)
    assert "no live timer" in h["timer_control"]({"action": "pause", "timer_id": tid}).lower()


def test_pausing_an_already_paused_timer_reports_it_did_not_apply(h, store):
    tid = store.add(600, "tea", now=NOW)
    store.pause(tid, NOW)
    assert "couldn't pause" in h["timer_control"]({"action": "pause", "label": "tea"}).lower()


@pytest.mark.parametrize("minutes", [None, 0, -2, True])
def test_extend_without_valid_minutes_is_an_error_and_changes_nothing(h, store, minutes):
    h["set_timer"]({"minutes": 10, "label": "tea"})
    args = {"action": "extend", "label": "tea"}
    if minutes is not None:
        args["minutes"] = minutes
    assert "error" in h["timer_control"](args).lower()
    assert store.list_live(NOW)[0]["due_ts"] == "2026-11-15T10:10:00"


def test_an_unknown_action_is_an_error(h):
    assert "error" in h["timer_control"]({"action": "obliterate"}).lower()


def test_a_non_integer_timer_id_is_an_error(h):
    assert "error" in h["timer_control"]({"action": "pause", "timer_id": "3"}).lower()


def test_control_without_a_timer_store_says_so_instead_of_raising():
    out = handlers(None)["timer_control"]({"action": "pause"})
    assert "error" in out.lower()


@pytest.mark.parametrize("action", ["extend", "snooze"])
@pytest.mark.parametrize("minutes", [1e30, 1e308])
def test_an_absurdly_large_duration_is_a_clean_error_not_an_exception(h, store, action, minutes):
    h["set_timer"]({"minutes": 10, "label": "tea"})
    tid = store.list_live(NOW)[0]["id"]
    if action == "snooze":
        store.mark_ringing(tid, NOW)
    result = h["timer_control"]({"action": action, "label": "tea", "minutes": minutes})
    assert result.startswith("Error:") and "minutes" in result
    assert store.get(tid)["status"] == ("ringing" if action == "snooze" else "running")


def test_extending_a_paused_timer_by_an_absurd_duration_is_a_clean_error(h, store):
    h["set_timer"]({"minutes": 10, "label": "tea"})
    h["timer_control"]({"action": "pause", "label": "tea"})
    result = h["timer_control"]({"action": "extend", "label": "tea", "minutes": 1e30})
    assert result.startswith("Error:") and "minutes" in result


# -- the schema must not lie to the model ------------------------------------


def _tool():
    from jarvisd.tools.reminders import TOOLS
    return next(t for t in TOOLS if t["name"] == "timer_control")


def test_schema_offers_rename_and_new_label_and_says_label_is_a_selector():
    tool = _tool()
    props = tool["input_schema"]["properties"]
    assert "rename" in props["action"]["enum"]
    assert "new_label" in props
    assert "rename" in tool["description"].lower()
    assert "new_label" in props["label"]["description"]
    assert "never renames" in props["label"]["description"].lower()


# -- a miss must say what is ringing -------------------------------------------


def test_unknown_label_with_a_ringing_timer_says_which_timer_is_ringing(h, store):
    tid = store.add(60, "", now=NOW)
    store.mark_ringing(tid, NOW)
    out = h["timer_control"]({"action": "dismiss", "label": "trash"})
    assert out == f"No live timer is labelled 'trash'. unnamed timer (#{tid}) is ringing now."
    assert store.get(tid)["status"] == "ringing"  # a miss reports; it never dismisses anything


def test_unknown_timer_id_with_a_ringing_timer_says_which_timer_is_ringing(h, store):
    tid = store.add(60, "tea", now=NOW)
    store.mark_ringing(tid, NOW)
    out = h["timer_control"]({"action": "dismiss", "timer_id": tid + 50})
    assert out == f"No live timer #{tid + 50}. tea (#{tid}) is ringing now."


def test_several_ringing_timers_are_all_listed_on_a_miss(h, store):
    a = store.add(60, "tea", now=NOW)
    b = store.add(60, "pasta", now=NOW)
    for t in (a, b):
        store.mark_ringing(t, NOW)
    out = h["timer_control"]({"action": "dismiss", "label": "ghost"})
    assert f"tea (#{a}) is ringing now." in out
    assert f"pasta (#{b}) is ringing now." in out


def test_only_ringing_timers_are_named_on_a_miss(h, store):
    ringing = store.add(60, "tea", now=NOW)
    store.mark_ringing(ringing, NOW)
    store.add(600, "pasta", now=NOW)  # running, not ringing
    out = h["timer_control"]({"action": "dismiss", "label": "ghost"})
    assert f"tea (#{ringing})" in out
    assert "pasta" not in out


def test_a_miss_with_nothing_ringing_is_unchanged(h, store):
    store.add(600, "pasta", now=NOW)
    assert h["timer_control"]({"action": "dismiss", "label": "ghost"}) == "No live timer is labelled 'ghost'."
    assert h["timer_control"]({"action": "dismiss", "timer_id": 99}) == "No live timer #99."


# -- rename must not create a duplicate live label ------------------------------


def test_rename_onto_another_live_timers_name_is_refused_case_insensitively(h, store):
    a = store.add(600, "tea", now=NOW)
    b = store.add(300, "pasta", now=NOW)
    out = h["timer_control"]({"action": "rename", "timer_id": b, "new_label": "TEA"})
    assert out.startswith("Error") and "tea" in out and f"#{a}" in out
    assert store.get(b)["label"] == "pasta"


def test_rename_to_its_own_name_is_not_a_conflict(h, store):
    a = store.add(600, "tea", now=NOW)
    out = h["timer_control"]({"action": "rename", "timer_id": a, "new_label": "Tea"})
    assert out == f"Renamed tea (#{a}) to 'Tea'."
    assert store.get(a)["label"] == "Tea"


@pytest.mark.parametrize("finish", ["dismiss", "cancel"])
def test_rename_onto_a_finished_timers_name_is_allowed(h, store, finish):
    old = store.add(600, "trash", now=NOW)
    getattr(store, finish)(old)
    b = store.add(300, "", now=NOW)
    out = h["timer_control"]({"action": "rename", "timer_id": b, "new_label": "trash"})
    assert out == f"Renamed unnamed timer (#{b}) to 'trash'."
    assert store.get(b)["label"] == "trash"


# -- stopping a ringing alarm is never blocked by a bad selector -----------------


def test_dismiss_with_a_stale_label_still_stops_a_ringing_timer_and_says_so(h, store):
    tid = store.add(60, "", now=NOW)
    store.mark_ringing(tid, NOW)
    out = h["timer_control"]({"action": "dismiss", "timer_id": tid, "label": "pasta"})
    assert out == f"Dismissed unnamed timer (#{tid}); it isn't labelled 'pasta'."
    assert store.get(tid)["status"] == "done"  # the alarm actually stopped
    assert not [r for r in store.list_live(NOW) if r["status"] == "ringing"]


def test_snooze_with_a_stale_label_still_silences_a_ringing_timer_and_says_so(h, store):
    tid = store.add(60, "tea", now=NOW)
    store.mark_ringing(tid, NOW)
    out = h["timer_control"]({"action": "snooze", "timer_id": tid, "label": "pasta", "minutes": 3})
    assert out == f"Snoozed tea (#{tid}) for 3 minutes; it isn't labelled 'pasta'."
    assert store.get(tid)["status"] == "running"


@pytest.mark.parametrize("pause_first", [False, True])
def test_dismiss_with_a_contradicting_label_is_refused_on_a_running_or_paused_timer(h, store, pause_first):
    tid = store.add(600, "tea", now=NOW)
    if pause_first:
        store.pause(tid, NOW)
    before = store.get(tid)["status"]
    out = h["timer_control"]({"action": "dismiss", "timer_id": tid, "label": "pasta"})
    assert out.startswith("Error") and "pasta" in out
    assert store.get(tid)["status"] == before


def test_extend_on_a_ringing_timer_with_a_contradicting_label_is_still_refused(h, store):
    tid = store.add(60, "tea", now=NOW)
    store.mark_ringing(tid, NOW)
    out = h["timer_control"]({"action": "extend", "timer_id": tid, "minutes": 1, "label": "pasta"})
    assert out.startswith("Error") and "pasta" in out


def test_a_refused_contradiction_names_the_timer_that_is_ringing(h, store):
    running = store.add(600, "pasta", now=NOW)
    ringing = store.add(60, "tea", now=NOW)
    store.mark_ringing(ringing, NOW)
    out = h["timer_control"]({"action": "dismiss", "timer_id": running, "label": "ghost"})
    assert out.startswith("Error")
    assert f"tea (#{ringing}) is ringing now." in out
    assert store.get(running)["status"] == "running"


def test_after_a_rename_the_old_name_still_stops_the_alarm(h, store):
    tid = store.add(60, "trash", now=NOW)
    h["timer_control"]({"action": "rename", "timer_id": tid, "new_label": "bin"})
    store.mark_ringing(tid, NOW)
    out = h["timer_control"]({"action": "dismiss", "timer_id": tid, "label": "trash"})
    assert out.startswith(f"Dismissed bin (#{tid})")
    assert store.get(tid)["status"] == "done"


def test_dismiss_by_a_shared_label_picks_the_one_ringing_timer(h, store):
    quiet = store.add(600, "tea", now=NOW)
    ringing = store.add(60, "tea", now=NOW)
    store.mark_ringing(ringing, NOW)
    out = h["timer_control"]({"action": "dismiss", "label": "tea"})
    assert out == f"Dismissed tea (#{ringing})."
    assert store.get(ringing)["status"] == "done"
    assert store.get(quiet)["status"] == "running"


def test_snooze_by_a_shared_label_picks_the_one_ringing_timer(h, store):
    store.add(600, "tea", now=NOW)
    ringing = store.add(60, "tea", now=NOW)
    store.mark_ringing(ringing, NOW)
    assert h["timer_control"]({"action": "snooze", "label": "tea"}).startswith(f"Snoozed tea (#{ringing})")


def test_a_shared_label_stays_ambiguous_when_nothing_or_several_ring_or_for_other_actions(h, store):
    a = store.add(600, "tea", now=NOW)
    b = store.add(600, "tea", now=NOW)
    assert h["timer_control"]({"action": "dismiss", "label": "tea"}).startswith("Which timer?")
    store.mark_ringing(a, NOW)
    store.mark_ringing(b, NOW)
    assert h["timer_control"]({"action": "dismiss", "label": "tea"}).startswith("Which timer?")
    assert store.get(a)["status"] == "ringing" and store.get(b)["status"] == "ringing"


def test_a_shared_label_with_one_ringing_is_still_ambiguous_for_cancel(h, store):
    store.add(600, "tea", now=NOW)
    ringing = store.add(60, "tea", now=NOW)
    store.mark_ringing(ringing, NOW)
    assert h["timer_control"]({"action": "cancel", "label": "tea"}).startswith("Which timer?")
    assert store.get(ringing)["status"] == "ringing"


@pytest.mark.parametrize("extra, note", [
    ({"label": 7}, "label"),
    ({"new_label": "x"}, "new_label"),
    ({"timer_id": "2"}, "timer_id"),
])
def test_junk_arguments_do_not_stop_a_ringing_dismiss(h, store, extra, note):
    tid = store.add(60, "", now=NOW)
    store.mark_ringing(tid, NOW)
    out = h["timer_control"]({"action": "dismiss", **extra})
    assert out.startswith(f"Dismissed unnamed timer (#{tid});")
    assert note in out
    assert store.get(tid)["status"] == "done"


@pytest.mark.parametrize("extra", [{"label": 7}, {"new_label": "x"}, {"timer_id": "2"}])
def test_junk_arguments_still_refuse_a_dismiss_of_a_running_timer(h, store, extra):
    tid = store.add(600, "", now=NOW)
    out = h["timer_control"]({"action": "dismiss", **extra})
    assert out.startswith("Error")
    assert store.get(tid)["status"] == "running"


def test_two_ringing_timers_stay_ambiguous_and_the_listing_includes_every_live_match(h, store):
    a = store.add(60, "", now=NOW)
    b = store.add(60, "", now=NOW)
    quiet = store.add(600, "pasta", now=NOW)
    store.mark_ringing(a, NOW)
    store.mark_ringing(b, NOW)
    out = h["timer_control"]({"action": "dismiss"})
    assert out.startswith("Which timer?")
    assert f"pasta (#{quiet})" in out
    assert store.get(a)["status"] == "ringing" and store.get(b)["status"] == "ringing"


# -- round 4 ---------------------------------------------------------------------


def test_a_blank_new_label_is_treated_as_absent_so_label_is_the_new_name(h, store):
    tid = store.add(600, "", now=NOW)
    out = h["timer_control"]({"action": "rename", "timer_id": tid, "label": "trash", "new_label": ""})
    assert out == f"Renamed unnamed timer (#{tid}) to 'trash'."
    assert store.get(tid)["label"] == "trash"


def test_rename_onto_a_paused_timers_name_is_refused(h, store):
    paused = store.add(600, "tea", now=NOW)
    store.pause(paused, NOW)
    other = store.add(300, "pasta", now=NOW)
    out = h["timer_control"]({"action": "rename", "timer_id": other, "new_label": "tea"})
    assert out.startswith("Error") and f"#{paused}" in out
    assert store.get(other)["label"] == "pasta"


def test_a_stored_label_with_stray_whitespace_is_still_found_and_still_collides(h, store, conn):
    tid = store.add(600, "tea", now=NOW)
    conn.execute("UPDATE timers SET label = ? WHERE id = ?", ("  Tea ", tid))
    conn.commit()
    other = store.add(300, "pasta", now=NOW)
    assert [r["id"] for r in store.find_by_label("tea")] == [tid]
    out = h["timer_control"]({"action": "rename", "timer_id": other, "new_label": "tea"})
    assert out.startswith("Error") and f"#{tid}" in out
    assert h["timer_control"]({"action": "pause", "timer_id": tid, "label": "tea"}).startswith("Paused")


def test_the_description_says_a_mismatching_label_is_an_error_only_when_it_is_a_selector():
    desc = _tool()["description"]
    assert "when label is used as a selector" in desc
    # The one shape where label next to timer_id is legitimate must not be called an error.
    assert "rename" in desc and "timer_id" in desc


# -- round 5: a named timer that is not the one ringing -------------------------


@pytest.fixture
def trash_running_other_ringing(store):
    """#1 'trash' is running; a different, unnamed timer #2 is ringing."""
    trash = store.add(600, "trash", now=NOW)
    ringing = store.add(60, "", now=NOW)
    store.mark_ringing(ringing, NOW)
    return trash, ringing


@pytest.mark.parametrize("args", [
    {"action": "dismiss", "label": "trash"},
    {"action": "dismiss", "timer_id": 1},
    {"action": "snooze", "label": "trash"},
])
def test_stopping_a_non_ringing_timer_while_another_rings_changes_nothing_and_names_the_ringer(
        h, store, trash_running_other_ringing, args):
    trash, ringing = trash_running_other_ringing
    out = h["timer_control"](args)
    assert out.startswith(f"trash (#{trash}) has ")  # states the named timer's own state
    assert "Nothing was changed" in out
    assert f"unnamed timer (#{ringing}) is ringing now." in out
    assert "cancel" in out  # how to end the named timer anyway
    assert store.get(trash)["status"] == "running"      # not destroyed
    assert store.get(ringing)["status"] == "ringing"    # the alarm is still going


def test_a_paused_timer_is_protected_the_same_way(h, store, trash_running_other_ringing):
    trash, ringing = trash_running_other_ringing
    store.pause(trash, NOW)
    out = h["timer_control"]({"action": "dismiss", "label": "trash"})
    assert "Nothing was changed" in out and f"(#{ringing}) is ringing now." in out
    assert store.get(trash)["status"] == "paused"


def test_the_model_can_then_dismiss_the_ringing_timer_by_id(h, store, trash_running_other_ringing):
    trash, ringing = trash_running_other_ringing
    h["timer_control"]({"action": "dismiss", "label": "trash"})
    assert h["timer_control"]({"action": "dismiss", "timer_id": ringing}).startswith("Dismissed")
    assert store.get(ringing)["status"] == "done"
    assert store.get(trash)["status"] == "running"


def test_cancel_acts_as_asked_and_says_what_is_ringing(h, store, trash_running_other_ringing):
    trash, ringing = trash_running_other_ringing
    out = h["timer_control"]({"action": "cancel", "label": "trash"})
    assert out == f"Cancelled trash (#{trash}). unnamed timer (#{ringing}) is ringing now."
    assert store.get(trash)["status"] == "cancelled"
    assert store.get(ringing)["status"] == "ringing"


def test_a_successful_pause_of_a_quiet_timer_still_says_what_is_ringing(h, store, trash_running_other_ringing):
    trash, ringing = trash_running_other_ringing
    out = h["timer_control"]({"action": "pause", "label": "trash"})
    assert out == f"Paused trash (#{trash}). unnamed timer (#{ringing}) is ringing now."


def test_dismissing_a_non_ringing_timer_still_works_when_nothing_rings(h, store):
    tid = store.add(600, "trash", now=NOW)
    assert h["timer_control"]({"action": "dismiss", "label": "trash"}) == f"Dismissed trash (#{tid})."
    assert store.get(tid)["status"] == "done"


def test_dismissing_one_of_two_ringing_timers_says_the_other_still_rings(h, store):
    a = store.add(60, "tea", now=NOW)
    b = store.add(60, "pasta", now=NOW)
    store.mark_ringing(a, NOW)
    store.mark_ringing(b, NOW)
    out = h["timer_control"]({"action": "dismiss", "timer_id": a})
    assert out == f"Dismissed tea (#{a}). pasta (#{b}) is ringing now."


# -- every refusal and failure names what is ringing -----------------------------


@pytest.fixture
def ringing_one(store):
    tid = store.add(60, "bell", now=NOW)
    store.mark_ringing(tid, NOW)
    return tid


@pytest.mark.parametrize("args", [
    {"action": "frobnicate"},
    {"action": "snooze", "minutes": -1},
    {"action": "extend", "minutes": "x", "label": "nothing"},
    {"action": "pause", "label": 7},
    {"action": "rename", "timer_id": 1},
    {"action": "dismiss", "label": "ghost"},
    {"action": "dismiss", "timer_id": 99},
])
def test_refusals_say_what_is_ringing(h, ringing_one, args):
    out = h["timer_control"](args)
    assert f"bell (#{ringing_one}) is ringing now." in out


def test_junk_args_on_a_quiet_timer_while_another_rings_still_name_the_ringer(h, store, ringing_one):
    quiet = store.add(600, "pasta", now=NOW)
    out = h["timer_control"]({"action": "pause", "timer_id": quiet, "new_label": "x"})
    assert out.startswith("Error") and f"bell (#{ringing_one}) is ringing now." in out


def test_a_mis_cased_action_is_accepted(h, store, ringing_one):
    assert h["timer_control"]({"action": " Dismiss "}) == f"Dismissed bell (#{ringing_one})."
    assert store.get(ringing_one)["status"] == "done"


def test_a_failed_snooze_still_says_the_timer_is_ringing(h, store, ringing_one, monkeypatch):
    monkeypatch.setattr(store, "snooze", lambda *a, **k: False)
    out = h["timer_control"]({"action": "snooze", "timer_id": ringing_one})
    assert out.startswith("Couldn't snooze") and f"bell (#{ringing_one}) is ringing now." in out


# -- the check and the write must not disagree -----------------------------------


def test_a_wrong_label_cannot_dismiss_a_timer_that_stopped_ringing_after_it_was_resolved(
        h, store, ringing_one, monkeypatch):
    real = store.dismiss_ringing

    def snoozed_first(tid):
        store.snooze(tid, 300, NOW)  # the tick loop got there between resolve and apply
        return real(tid)

    monkeypatch.setattr(store, "dismiss_ringing", snoozed_first)
    out = h["timer_control"]({"action": "dismiss", "timer_id": ringing_one, "label": "wrong"})
    assert out.startswith("Couldn't dismiss")
    assert store.get(ringing_one)["status"] == "running"


# -- the description corrections are pinned by their substance -------------------


def test_description_pins_the_rename_shorthand_and_the_ringing_exception():
    desc = _tool()["description"]
    assert ("rename with a timer_id and only 'label' also works: the label is then the new name") in desc
    assert ("when label is used as a selector next to a timer_id that it does not match, that is an error, "
            "except that dismiss or snooze of a RINGING timer still stops it and says the label did not match") in desc


def test_label_property_says_it_names_a_timer_only_through_rename():
    prop = _tool()["input_schema"]["properties"]["label"]["description"]
    assert "Except for rename it never renames a timer: to name one, use action 'rename' with new_label." in prop


def test_junk_args_on_a_quiet_timer_for_a_stopper_while_another_rings_name_the_ringer(h, store, ringing_one):
    quiet = store.add(600, "pasta", now=NOW)
    out = h["timer_control"]({"action": "dismiss", "timer_id": quiet, "new_label": "x"})
    assert out.startswith("Error") and f"bell (#{ringing_one}) is ringing now." in out
    assert store.get(quiet)["status"] == "running"


def test_description_says_dismiss_of_a_quiet_timer_changes_nothing_while_another_rings():
    assert ("unless a different timer is ringing: then nothing changes and the ringing one is named; "
            "use 'cancel' to end the quiet one anyway") in _tool()["description"]
