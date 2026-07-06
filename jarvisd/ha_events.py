"""Home Assistant WebSocket event listener: fires trigger rules through the guard.

Direct rules execute a stored tool call through Assistant.execute_guarded
(via the injected `run_direct` callable); smart rules go through
Assistant.process (via the injected `run_smart` callable). Both callables are
async and are created by the caller (server/bootstrap wiring), which is
responsible for taking the assistant lock and hopping to a worker thread --
this module never touches the assistant or its lock directly, which is what
keeps it unit-testable without opening a socket.
"""

import asyncio
import json
import logging
from datetime import datetime
from urllib.parse import urlsplit, urlunsplit

import websockets

log = logging.getLogger(__name__)

MIN_BACKOFF_SEC = 5
MAX_BACKOFF_SEC = 60


def _ws_url(ha_url: str) -> str:
    """Derive HA's WebSocket API URL (ws(s)://host:port/api/websocket) from its http(s) base_url."""
    parts = urlsplit(ha_url)
    scheme = "wss" if parts.scheme == "https" else "ws"
    return urlunsplit((scheme, parts.netloc, "/api/websocket", "", ""))


class HAEventListener:
    """Connects to Home Assistant's WebSocket API and fires matching trigger rules."""

    def __init__(
        self,
        ha_url: str,
        ha_token: str,
        rules_store,
        notifier,
        run_direct,
        run_smart,
        now=datetime.now,
    ):
        self.ha_url = ha_url
        self.ha_token = ha_token
        self.rules_store = rules_store
        self.notifier = notifier
        self.run_direct = run_direct
        self.run_smart = run_smart
        self.now = now
        self._connected = False  # tracks connect/disconnect transitions for logging

    # -- event processing (fully testable without a socket) --

    async def _process_event(self, event: dict) -> None:
        """Handle one HA WebSocket `{"type": "event", "event": {...}}` message.

        Reduces the event to `{event_type, entity_id, to_state}`, finds
        matching enabled rules, and fires each one whose cooldown allows it.
        Never raises -- a malformed event or a bad rule must not kill the
        listener loop.
        """
        inner = event.get("event") or {}
        event_type = inner.get("event_type")
        if event_type is None:
            return

        data = inner.get("data") or {}
        entity_id = data.get("entity_id")
        new_state = data.get("new_state") or {}
        to_state = new_state.get("state")

        reduced = {"event_type": event_type, "entity_id": entity_id, "to_state": to_state}

        for rule in self.rules_store.matches(reduced):
            if not self.rules_store.check_and_stamp_cooldown(rule["id"], self.now()):
                continue
            await self._fire_rule(rule, entity_id, to_state)

    async def _fire_rule(self, rule, entity_id, to_state) -> None:
        name = rule["name"]
        kind = rule["kind"]

        if kind == "direct":
            try:
                action = json.loads(rule["action"])
            except json.JSONDecodeError as e:
                log.error("Rule '%s': malformed action JSON, skipping: %s", name, e)
                return
            try:
                result = await self.run_direct(action)
            except Exception:
                # A failing handler must NOT tear down the socket (which would drop
                # every other rule's events during reconnect) -- log and skip just
                # this rule, mirroring the malformed-JSON handling above.
                log.exception("Rule '%s': direct action failed, skipping", name)
                return
            self.notifier.notify(f"Rule '{name}': {result}", "low")
        elif kind == "smart":
            prompt = (
                f"[Automation rule '{name}' triggered by {entity_id} -> {to_state}] "
                f"{rule['action']}"
            )
            try:
                result = await self.run_smart(prompt)
            except Exception:
                log.exception("Rule '%s': smart action failed, skipping", name)
                return
            self.notifier.notify(result, "high")
        else:
            log.error("Rule '%s': unknown kind %r, skipping", name, kind)

    # -- socket handling (thin wrapper around _process_event) --

    async def _handshake(self, ws) -> None:
        """Perform the HA auth handshake and subscribe to state_changed events."""
        first = json.loads(await ws.recv())
        if first.get("type") == "auth_required":
            await ws.send(json.dumps({"type": "auth", "access_token": self.ha_token}))
            auth_result = json.loads(await ws.recv())
            if auth_result.get("type") != "auth_ok":
                raise ConnectionError(f"HA auth failed: {auth_result}")
        await ws.send(json.dumps({"id": 1, "type": "subscribe_events", "event_type": "state_changed"}))
        await ws.recv()  # ack for the subscribe_events call

    async def run(self) -> None:
        """Long-running task: connect, handshake, subscribe, dispatch events forever.

        Reconnects with exponential backoff (5s -> 60s cap) on any failure;
        never raises out of the task (except on cancellation, which must
        propagate for clean shutdown). Logs an INFO line on connect/disconnect
        transitions only -- not once per retry attempt.
        """
        backoff = MIN_BACKOFF_SEC
        url = _ws_url(self.ha_url)
        connect_failure_logged = False  # keeps a wrong-token loop from spamming the log
        while True:
            try:
                async with websockets.connect(url) as ws:
                    await self._handshake(ws)
                    self._connected = True
                    connect_failure_logged = False
                    backoff = MIN_BACKOFF_SEC
                    log.info("Connected to Home Assistant WebSocket at %s", url)
                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                        except json.JSONDecodeError:
                            log.warning("Non-JSON message from Home Assistant, skipping")
                            continue
                        if msg.get("type") == "event":
                            await self._process_event(msg)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if self._connected:
                    # We were up and lost the connection: a real transition, log it.
                    log.info("Disconnected from Home Assistant WebSocket: %s", e)
                elif not connect_failure_logged:
                    # First failure of a never-succeeded connect (e.g. HA down or a
                    # bad token): log once, then stay quiet across the retry loop.
                    log.info("Could not connect to Home Assistant WebSocket (will keep retrying): %s", e)
                    connect_failure_logged = True
            self._connected = False
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF_SEC)
