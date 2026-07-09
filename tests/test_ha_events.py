"""Tests for HAEventListener -- HA event -> rule match -> guarded execution.

All tests drive `_process_event` directly with plain dicts (no sockets), per
the Task 6 brief. `run_direct`/`run_smart` are represented by async spy
callables standing in for the real callables that server/bootstrap wiring
would create (which take the assistant lock and call
`assistant.execute_guarded` / `assistant.process` via `asyncio.to_thread`).
"""

import asyncio
import json
import logging
import sqlite3
import threading

import pytest

from jarvisd.db import connect
from jarvisd.ha_events import HAEventListener, _ws_url
from jarvisd.rules_store import RulesStore


class SpyNotifier:
    def __init__(self):
        self.calls = []

    def notify(self, text, priority="low"):
        self.calls.append((text, priority))


class SpyCallable:
    """Async spy: records what it was awaited with, returns a canned result."""

    def __init__(self, result="ok"):
        self.calls = []
        self.result = result

    async def __call__(self, arg):
        self.calls.append(arg)
        return self.result


@pytest.fixture
def conn():
    c = connect(":memory:")
    yield c
    c.close()


@pytest.fixture
def rules(conn):
    return RulesStore(conn)


def make_listener(rules_store, notifier=None, run_direct=None, run_smart=None):
    return HAEventListener(
        "http://homeassistant.local:8123",
        "fake-token",
        rules_store,
        notifier if notifier is not None else SpyNotifier(),
        run_direct if run_direct is not None else SpyCallable(),
        run_smart if run_smart is not None else SpyCallable(),
    )


def state_event(entity_id, state, event_type="state_changed"):
    return {
        "type": "event",
        "event": {
            "event_type": event_type,
            "data": {"entity_id": entity_id, "new_state": {"state": state}},
        },
    }


# --- ws url derivation ---


def test_ws_url_http_to_ws():
    assert _ws_url("http://homeassistant.local:8123") == "ws://homeassistant.local:8123/api/websocket"


def test_ws_url_https_to_wss():
    assert _ws_url("https://ha.example.com") == "wss://ha.example.com/api/websocket"


# --- matching / filtering / no-op ---


def test_empty_rules_store_is_noop(rules):
    run_direct = SpyCallable()
    listener = make_listener(rules, run_direct=run_direct)

    asyncio.run(listener._process_event(state_event("motion.hall", "on")))

    assert run_direct.calls == []


def test_filters_by_entity_and_state(rules):
    action = json.dumps({"tool": "control_light", "input": {"entity_id": "light.hall", "action": "turn_on"}})
    rules.add("Hall light", "state_changed", "direct", action, entity_id="motion.hall", to_state="on")
    run_direct = SpyCallable()
    listener = make_listener(rules, run_direct=run_direct)

    # wrong entity -> no fire
    asyncio.run(listener._process_event(state_event("motion.lounge", "on")))
    assert run_direct.calls == []

    # wrong state -> no fire
    asyncio.run(listener._process_event(state_event("motion.hall", "off")))
    assert run_direct.calls == []

    # matches -> fires
    asyncio.run(listener._process_event(state_event("motion.hall", "on")))
    assert run_direct.calls == [{"tool": "control_light", "input": {"entity_id": "light.hall", "action": "turn_on"}}]


def test_unmatched_event_type_is_noop(rules):
    action = json.dumps({"tool": "test", "input": {}})
    rules.add("Rule", "state_changed", "direct", action)
    run_direct = SpyCallable()
    listener = make_listener(rules, run_direct=run_direct)

    asyncio.run(listener._process_event(state_event("x", "y", event_type="service_called")))

    assert run_direct.calls == []


# --- cooldown ---


def test_cooldown_blocks_second_fire(rules):
    action = json.dumps({"tool": "control_light", "input": {}})
    rules.add("Rule", "state_changed", "direct", action, entity_id="motion.hall", cooldown_sec=300)
    run_direct = SpyCallable()
    listener = make_listener(rules, run_direct=run_direct)

    asyncio.run(listener._process_event(state_event("motion.hall", "on")))
    asyncio.run(listener._process_event(state_event("motion.hall", "on")))

    assert len(run_direct.calls) == 1


# --- direct rules ---


def test_direct_rule_calls_run_direct_with_parsed_json_and_notifies(rules):
    action = json.dumps({"tool": "control_light", "input": {"entity_id": "light.hall", "action": "turn_on"}})
    rules.add("Hall light on motion", "state_changed", "direct", action, entity_id="motion.hall", to_state="on")
    run_direct = SpyCallable(result="Light turned on")
    notifier = SpyNotifier()
    listener = make_listener(rules, notifier=notifier, run_direct=run_direct)

    asyncio.run(listener._process_event(state_event("motion.hall", "on")))

    assert run_direct.calls == [{"tool": "control_light", "input": {"entity_id": "light.hall", "action": "turn_on"}}]
    assert notifier.calls == [("Rule 'Hall light on motion': Light turned on", "low")]


# --- smart rules ---


def test_smart_rule_calls_run_smart_with_composed_prompt_and_notifies_high(rules):
    rules.add(
        "Notify about motion",
        "state_changed",
        "smart",
        "Send a notification that motion was detected",
        entity_id="motion.hall",
        to_state="on",
    )
    run_smart = SpyCallable(result="Notification sent.")
    notifier = SpyNotifier()
    listener = make_listener(rules, notifier=notifier, run_smart=run_smart)

    asyncio.run(listener._process_event(state_event("motion.hall", "on")))

    assert run_smart.calls == [
        "[Automation rule 'Notify about motion' triggered by motion.hall -> on] "
        "Send a notification that motion was detected"
    ]
    assert notifier.calls == [("Notification sent.", "high")]


# --- malformed direct action JSON ---


def test_malformed_direct_action_json_is_skipped_not_crashed(rules, caplog):
    # RulesStore.add() validates direct actions and would reject this; insert
    # directly to simulate data that became invalid after storage (or a future
    # schema mismatch) -- the listener must log and skip, never crash.
    rules._conn.execute(
        """INSERT INTO trigger_rules
        (name, event_type, entity_id, to_state, kind, action, cooldown_sec, enabled, created_ts)
        VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)""",
        ("Broken rule", "state_changed", "motion.hall", "on", "direct", "not json", 300, "2026-01-01T00:00:00"),
    )
    rules._conn.commit()
    run_direct = SpyCallable()
    listener = make_listener(rules, run_direct=run_direct)

    with caplog.at_level(logging.ERROR):
        asyncio.run(listener._process_event(state_event("motion.hall", "on")))

    assert run_direct.calls == []
    assert any("Broken rule" in r.message for r in caplog.records)


# --- a failing handler must not tear down the listener / block other rules ---


def test_run_direct_exception_does_not_block_other_matched_rules(rules, caplog):
    """One rule's handler raising must skip only that rule -- the listener survives
    (returns normally, no exception out of _process_event) and other matched rules
    still fire. Otherwise the exception would propagate to run() and drop the whole
    socket during a reconnect, starving every other rule of events."""
    boom_action = json.dumps({"tool": "boom", "input": {}})
    ok_action = json.dumps({"tool": "control_light", "input": {"entity_id": "light.hall"}})
    # matches() orders by created_ts DESC; both fire regardless of order.
    rules.add("Boom rule", "state_changed", "direct", boom_action, entity_id="motion.hall")
    rules.add("Good rule", "state_changed", "direct", ok_action, entity_id="motion.hall")

    class ExplodingOnBoom:
        def __init__(self):
            self.calls = []

        async def __call__(self, action):
            self.calls.append(action)
            if action["tool"] == "boom":
                raise RuntimeError("handler blew up")
            return "Light on"

    run_direct = ExplodingOnBoom()
    notifier = SpyNotifier()
    listener = make_listener(rules, notifier=notifier, run_direct=run_direct)

    with caplog.at_level(logging.ERROR):
        # Must not raise despite the boom rule's handler exploding.
        asyncio.run(listener._process_event(state_event("motion.hall", "on")))

    # Both rules were attempted; the good one still notified; the bad one was logged.
    assert len(run_direct.calls) == 2
    assert notifier.calls == [("Rule 'Good rule': Light on", "low")]
    assert any("Boom rule" in r.message for r in caplog.records)


# --- notify() must run off the event-loop thread ---


def test_notify_runs_off_the_event_loop_thread(rules):
    """notifier.notify() does sqlite I/O, possibly a blocking HA HTTP call, and --
    critically -- its push_to_clients bridge blocks on
    run_coroutine_threadsafe(...).result(), which DEADLOCKS the whole daemon if
    invoked on the loop thread. _fire_rule must therefore hop to a worker thread
    (asyncio.to_thread) for every notify, for both direct and smart rules."""
    direct_action = json.dumps({"tool": "control_light", "input": {}})
    rules.add("Direct rule", "state_changed", "direct", direct_action, entity_id="motion.hall")
    rules.add("Smart rule", "state_changed", "smart", "say something", entity_id="motion.hall")

    class ThreadRecordingNotifier:
        def __init__(self):
            self.calls = []

        def notify(self, text, priority="low"):
            self.calls.append(threading.get_ident())

    notifier = ThreadRecordingNotifier()
    listener = make_listener(rules, notifier=notifier)

    async def scenario():
        await listener._process_event(state_event("motion.hall", "on"))
        return threading.get_ident()

    loop_thread = asyncio.run(scenario())

    assert len(notifier.calls) == 2  # one per rule kind
    assert all(t != loop_thread for t in notifier.calls)


# --- store failures during matching must not kill the listener ---


def test_rules_store_failure_during_matching_is_contained(caplog):
    """A sqlite hiccup in matches() must be logged and skipped -- if it raised out
    of _process_event, run() would drop the HA socket and reconnect, starving
    every rule of events over a transient DB error."""

    class ExplodingRulesStore:
        def matches(self, event):
            raise sqlite3.OperationalError("database is locked")

    listener = HAEventListener(
        "http://homeassistant.local:8123", "fake-token",
        ExplodingRulesStore(), SpyNotifier(), SpyCallable(), SpyCallable(),
    )

    with caplog.at_level(logging.ERROR):
        # Must not raise.
        asyncio.run(listener._process_event(state_event("motion.hall", "on")))

    assert any("match" in r.message.lower() for r in caplog.records)


def test_cooldown_check_failure_skips_rule_not_listener(rules, caplog):
    action = json.dumps({"tool": "control_light", "input": {}})
    rules.add("Rule", "state_changed", "direct", action, entity_id="motion.hall")
    run_direct = SpyCallable()
    listener = make_listener(rules, run_direct=run_direct)

    def boom(rule_id, now):
        raise sqlite3.OperationalError("disk I/O error")

    rules.check_and_stamp_cooldown = boom

    with caplog.at_level(logging.ERROR):
        # Must not raise; the rule is skipped, not fired.
        asyncio.run(listener._process_event(state_event("motion.hall", "on")))

    assert run_direct.calls == []
    assert any("Rule" in r.message for r in caplog.records)
