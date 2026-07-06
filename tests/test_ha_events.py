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
