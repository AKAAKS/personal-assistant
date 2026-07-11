import asyncio
import json
from datetime import datetime

import websockets

from jarvisd.server import JarvisServer


class FakeAssistant:
    def process(self, text):
        return f"echo: {text}"

    def reset_conversation(self):
        pass


class DrainingNotifier:
    """Fake notifier whose drain() returns a fixed batch exactly once."""

    def __init__(self, notices):
        self._notices = notices
        self.drain_calls = 0

    def drain(self):
        self.drain_calls += 1
        notices, self._notices = self._notices, []
        return notices

    def notify(self, text, priority="low"):
        pass


class FakeProposalStore:
    def __init__(self, pending):
        self._pending = pending
        self.list_pending_calls = 0

    def list_pending(self):
        self.list_pending_calls += 1
        return self._pending


def run_client(server, scenario):
    async def _run():
        async with websockets.serve(server.handle, "127.0.0.1", 0) as ws_server:
            port = ws_server.sockets[0].getsockname()[1]
            async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
                return await scenario(ws)
    return asyncio.run(_run())


# --- _habit_tick ---


def test_habit_tick_calls_habit_scan():
    calls = []

    def fake_scan():
        calls.append(1)
        return 2

    server = JarvisServer(FakeAssistant(), habit_scan=fake_scan)

    async def _run():
        task = asyncio.create_task(server._habit_tick())
        await asyncio.sleep(0.05)
        alive = not task.done()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return alive

    alive = asyncio.run(_run())
    assert calls == [1]
    assert alive is True


def test_habit_tick_survives_habit_scan_exception():
    def boom():
        raise RuntimeError("boom")

    server = JarvisServer(FakeAssistant(), habit_scan=boom)

    async def _run():
        task = asyncio.create_task(server._habit_tick())
        await asyncio.sleep(0.05)
        alive = not task.done()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return alive

    assert asyncio.run(_run()) is True


def test_seconds_until_next_scan_is_positive_and_bounded_by_a_day(monkeypatch):
    monkeypatch.setenv("HABIT_SCAN_HOUR", "3")
    server = JarvisServer(FakeAssistant())
    now = datetime(2026, 7, 10, 14, 30, 0)  # afternoon; next 3am is tomorrow
    seconds = server._seconds_until_next_scan(now)
    assert 0 < seconds <= 24 * 3600


def test_seconds_until_next_scan_same_day_when_before_hour(monkeypatch):
    monkeypatch.setenv("HABIT_SCAN_HOUR", "3")
    server = JarvisServer(FakeAssistant())
    now = datetime(2026, 7, 10, 1, 0, 0)  # before 3am -> today's 3am is next
    seconds = server._seconds_until_next_scan(now)
    assert seconds == 2 * 3600


def test_seconds_until_next_scan_tolerates_malformed_hour_env(monkeypatch):
    # A bad HABIT_SCAN_HOUR must NOT raise: this runs outside _habit_tick's
    # try/except, so a raise here would silently kill the nightly task.
    server = JarvisServer(FakeAssistant())
    now = datetime(2026, 7, 10, 1, 0, 0)
    for bad in ("3am", "25", "-1", "", "not-a-number"):
        monkeypatch.setenv("HABIT_SCAN_HOUR", bad)
        seconds = server._seconds_until_next_scan(now)  # must not raise
        assert 0 < seconds <= 24 * 3600
    # Falls back to the 3am default -> 2 hours from 1am.
    monkeypatch.setenv("HABIT_SCAN_HOUR", "3am")
    assert server._seconds_until_next_scan(now) == 2 * 3600


# --- serve_forever wiring ---


def test_serve_forever_starts_habit_task_only_when_habit_scan_set():
    server = JarvisServer(FakeAssistant(), habit_scan=lambda: 0)

    async def _run():
        task = asyncio.create_task(server.serve_forever())
        await asyncio.sleep(0.05)
        started = server._habit_task is not None
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return started

    assert asyncio.run(_run()) is True


def test_serve_forever_does_not_start_habit_task_when_habit_scan_none():
    server = JarvisServer(FakeAssistant())

    async def _run():
        task = asyncio.create_task(server.serve_forever())
        await asyncio.sleep(0.05)
        started = server._habit_task is not None
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return started

    assert asyncio.run(_run()) is False


# --- surfacing trailer ---


def test_text_in_appends_habit_trailer_when_pending_nonempty():
    proposal_store = FakeProposalStore([{"id": 1}, {"id": 2}])

    async def scenario(ws):
        await ws.send(json.dumps({"type": "text_in", "text": "hi"}))
        return json.loads(await ws.recv())

    server = JarvisServer(FakeAssistant(), proposal_store=proposal_store)
    reply = run_client(server, scenario)
    assert "2 habit suggestion" in reply["text"]
    assert "show suggestions" in reply["text"]
    assert proposal_store.list_pending_calls == 1


def test_text_in_no_habit_trailer_when_pending_empty():
    proposal_store = FakeProposalStore([])

    async def scenario(ws):
        await ws.send(json.dumps({"type": "text_in", "text": "hi"}))
        return json.loads(await ws.recv())

    server = JarvisServer(FakeAssistant(), proposal_store=proposal_store)
    reply = run_client(server, scenario)
    assert reply["text"] == "echo: hi"


def test_text_in_no_habit_trailer_when_proposal_store_none():
    async def scenario(ws):
        await ws.send(json.dumps({"type": "text_in", "text": "hi"}))
        return json.loads(await ws.recv())

    server = JarvisServer(FakeAssistant())
    reply = run_client(server, scenario)
    assert reply["text"] == "echo: hi"


def test_habit_trailer_coexists_with_drained_notices():
    notifier = DrainingNotifier(["milk", "trash day"])
    proposal_store = FakeProposalStore([{"id": 1}])

    async def scenario(ws):
        await ws.send(json.dumps({"type": "text_in", "text": "hi"}))
        return json.loads(await ws.recv())

    server = JarvisServer(FakeAssistant(), notifier=notifier, proposal_store=proposal_store)
    reply = run_client(server, scenario)
    assert "While you were away:" in reply["text"]
    assert "- milk" in reply["text"]
    assert "- trash day" in reply["text"]
    assert "1 habit suggestion" in reply["text"]
    assert "show suggestions" in reply["text"]
