import asyncio
import json
from datetime import datetime

import pytest
import websockets

from jarvisd import db
from jarvisd.notifier import Notifier
from jarvisd.reminder_store import ReminderStore
from jarvisd.server import JarvisServer


class FakeAssistant:
    def __init__(self):
        self.was_reset = False

    def process(self, text):
        return f"echo: {text}"

    def reset_conversation(self):
        self.was_reset = True


class FakeTTS:
    def synthesize(self, text):
        return b"RIFF-fake-wav:" + text.encode()


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


class BrokenWS:
    """Simulates a client whose connection died between registration and send."""

    async def send(self, *_args):
        raise RuntimeError("boom, connection is dead")


async def _wait_for_client_count(server, n, timeout=2.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while len(server._clients) < n:
        if loop.time() > deadline:
            raise TimeoutError(f"server never reached {n} client(s)")
        await asyncio.sleep(0.01)


def run_client(server, scenario):
    async def _run():
        async with websockets.serve(server.handle, "127.0.0.1", 0) as ws_server:
            port = ws_server.sockets[0].getsockname()[1]
            async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
                return await scenario(ws)
    return asyncio.run(_run())


# --- push_proactive ---


def test_push_proactive_reaches_connected_client_no_tts():
    async def _run():
        server = JarvisServer(FakeAssistant())
        async with websockets.serve(server.handle, "127.0.0.1", 0) as ws_server:
            port = ws_server.sockets[0].getsockname()[1]
            async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
                await _wait_for_client_count(server, 1)
                reached = await server.push_proactive("wake up")
                msg = json.loads(await ws.recv())
                return reached, msg

    reached, msg = asyncio.run(_run())
    assert reached == 1
    assert msg == {"type": "proactive_say", "text": "wake up", "audio_follows": False}


def test_push_proactive_with_tts_sends_wav_frame():
    async def _run():
        server = JarvisServer(FakeAssistant(), tts=FakeTTS())
        async with websockets.serve(server.handle, "127.0.0.1", 0) as ws_server:
            port = ws_server.sockets[0].getsockname()[1]
            async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
                await _wait_for_client_count(server, 1)
                reached = await server.push_proactive("wake up")
                msg = json.loads(await ws.recv())
                wav = await ws.recv()
                return reached, msg, wav

    reached, msg, wav = asyncio.run(_run())
    assert reached == 1
    assert msg == {"type": "proactive_say", "text": "wake up", "audio_follows": True}
    assert wav == b"RIFF-fake-wav:wake up"


def test_push_proactive_returns_zero_with_no_clients():
    server = JarvisServer(FakeAssistant())
    reached = asyncio.run(server.push_proactive("anyone there?"))
    assert reached == 0


def test_push_proactive_skips_broken_client_and_reaches_others():
    async def _run():
        server = JarvisServer(FakeAssistant())
        async with websockets.serve(server.handle, "127.0.0.1", 0) as ws_server:
            port = ws_server.sockets[0].getsockname()[1]
            async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
                await _wait_for_client_count(server, 1)
                broken = BrokenWS()
                server._clients[broken] = asyncio.Lock()
                reached = await server.push_proactive("hi")
                msg = json.loads(await ws.recv())
                still_registered = broken in server._clients
                return reached, msg, still_registered

    reached, msg, still_registered = asyncio.run(_run())
    assert reached == 1
    assert msg["type"] == "proactive_say"
    assert still_registered is False


# --- thread-safe bridge ---


def test_push_from_thread_returns_zero_when_loop_not_set():
    server = JarvisServer(FakeAssistant())
    assert server._push_from_thread("x") == 0


def test_push_from_thread_bridges_to_connected_client():
    async def _run():
        server = JarvisServer(FakeAssistant())
        async with websockets.serve(server.handle, "127.0.0.1", 0) as ws_server:
            port = ws_server.sockets[0].getsockname()[1]
            server._loop = asyncio.get_running_loop()
            async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
                await _wait_for_client_count(server, 1)
                reached = await asyncio.to_thread(server._push_from_thread, "hi from thread")
                msg = json.loads(await ws.recv())
                return reached, msg

    reached, msg = asyncio.run(_run())
    assert reached == 1
    assert msg["type"] == "proactive_say"
    assert msg["text"] == "hi from thread"


def test_serve_forever_wires_push_to_clients_and_starts_reminder_task():
    class NoOpNotifier:
        def __init__(self):
            self.push_to_clients = None

        def drain(self):
            return []

    class EmptyReminderStore:
        def due(self, now):
            return []

    notifier = NoOpNotifier()
    server = JarvisServer(FakeAssistant(), notifier=notifier, reminder_store=EmptyReminderStore())

    async def _run():
        task = asyncio.create_task(server.serve_forever())
        await asyncio.sleep(0.05)
        assert notifier.push_to_clients == server._push_from_thread
        assert server._loop is not None
        assert server._reminder_task is not None
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(_run())


# --- scheduler tick ---


def test_fire_due_reminders_fires_once_and_advances_daily_recurrence(tmp_path):
    conn = db.connect(str(tmp_path / "jarvis.db"))
    reminder_store = ReminderStore(conn)
    notifier = Notifier(conn, quiet_hours="")  # no clients/HA -> high priority queues

    long_past = datetime(2000, 1, 1, 0, 0, 0)
    rid = reminder_store.add("stretch", long_past.isoformat(timespec="seconds"), recurrence="daily")

    server = JarvisServer(FakeAssistant(), notifier=notifier, reminder_store=reminder_store)

    before = datetime.now()
    asyncio.run(server._fire_due_reminders())

    notices = notifier.drain()
    assert notices == ["Reminder: stretch"]

    row = conn.execute("SELECT due_ts, status FROM reminders WHERE id=?", (rid,)).fetchone()
    assert row["status"] == "active"
    new_due = datetime.fromisoformat(row["due_ts"])
    assert new_due > before  # advanced past "now" (catch-up loop), not still long_past
    assert new_due > long_past

    # Second tick immediately after: due_ts is now in the future, so nothing re-fires.
    asyncio.run(server._fire_due_reminders())
    assert notifier.drain() == []


def test_fire_due_reminders_none_recurrence_marks_done_and_fires_once(tmp_path):
    conn = db.connect(str(tmp_path / "jarvis.db"))
    reminder_store = ReminderStore(conn)
    notifier = Notifier(conn, quiet_hours="")

    long_past = datetime(2000, 1, 1, 0, 0, 0)
    rid = reminder_store.add("call mom", long_past.isoformat(timespec="seconds"))

    server = JarvisServer(FakeAssistant(), notifier=notifier, reminder_store=reminder_store)
    asyncio.run(server._fire_due_reminders())

    assert notifier.drain() == ["Reminder: call mom"]
    row = conn.execute("SELECT status FROM reminders WHERE id=?", (rid,)).fetchone()
    assert row["status"] == "done"

    # Done reminders never come up again.
    asyncio.run(server._fire_due_reminders())
    assert notifier.drain() == []


def test_reminder_tick_survives_fire_due_reminders_exception():
    server = JarvisServer(FakeAssistant())

    async def boom():
        raise RuntimeError("boom")

    server._fire_due_reminders = boom

    async def _run():
        task = asyncio.create_task(server._reminder_tick())
        await asyncio.sleep(0.05)
        alive = not task.done()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return alive

    assert asyncio.run(_run()) is True


# --- notice drain wiring ---


def test_text_in_appends_drained_notices_to_reply():
    notifier = DrainingNotifier(["milk", "trash day"])

    async def scenario(ws):
        await ws.send(json.dumps({"type": "text_in", "text": "hi"}))
        return json.loads(await ws.recv())

    reply = run_client(JarvisServer(FakeAssistant(), notifier=notifier), scenario)
    assert reply["text"] == "echo: hi\n\nWhile you were away:\n- milk\n- trash day"
    assert notifier.drain_calls == 1


def test_audio_in_appends_drained_notices_to_reply():
    class FakeSTT:
        def transcribe(self, audio):
            return "turn on the light"

    import numpy as np

    notifier = DrainingNotifier(["someone rang the doorbell"])

    async def scenario(ws):
        await ws.send(np.zeros(1600, dtype=np.int16).tobytes())
        return json.loads(await ws.recv())

    server = JarvisServer(FakeAssistant(), stt=FakeSTT(), notifier=notifier)
    reply = run_client(server, scenario)
    assert reply["text"] == "echo: turn on the light\n\nWhile you were away:\n- someone rang the doorbell"
    assert notifier.drain_calls == 1


def test_text_in_with_no_pending_notices_is_unmodified():
    notifier = DrainingNotifier([])

    async def scenario(ws):
        await ws.send(json.dumps({"type": "text_in", "text": "hi"}))
        return json.loads(await ws.recv())

    reply = run_client(JarvisServer(FakeAssistant(), notifier=notifier), scenario)
    assert reply["text"] == "echo: hi"
    assert notifier.drain_calls == 1


def test_reset_does_not_drain_notices():
    notifier = DrainingNotifier(["should not appear"])
    fake = FakeAssistant()

    async def scenario(ws):
        await ws.send(json.dumps({"type": "reset"}))
        return json.loads(await ws.recv())

    reply = run_client(JarvisServer(fake, notifier=notifier), scenario)
    assert reply["text"] == "Conversation cleared."
    assert fake.was_reset
    assert notifier.drain_calls == 0


def test_no_notifier_does_not_touch_reply():
    async def scenario(ws):
        await ws.send(json.dumps({"type": "text_in", "text": "hi"}))
        return json.loads(await ws.recv())

    reply = run_client(JarvisServer(FakeAssistant()), scenario)
    assert reply["text"] == "echo: hi"


# --- per-connection write serialization ---


class GatedWS:
    """Records frames; blocks (before recording) on one specific payload until gated."""

    def __init__(self, gate: asyncio.Event, block_on: bytes):
        self.frames = []
        self.gate = gate
        self.block_on = block_on

    async def send(self, payload):
        if isinstance(payload, (bytes, bytearray)) and bytes(payload) == self.block_on:
            await self.gate.wait()
        self.frames.append(payload)


def test_concurrent_reply_and_push_never_split_json_wav_pairs():
    """A proactive push racing a reply on the SAME connection must not interleave
    frames: the client would misattribute WAV audio. Frame pairs stay atomic."""

    async def _run():
        server = JarvisServer(FakeAssistant(), tts=FakeTTS())
        gate = asyncio.Event()
        ws = GatedWS(gate, block_on=b"RIFF-fake-wav:the reply")
        server._clients[ws] = asyncio.Lock()

        reply_task = asyncio.create_task(
            server._send_reply(ws, "the reply", heard=None, speak=True)
        )
        await asyncio.sleep(0.05)  # _send_reply has sent its JSON, is blocked on the WAV
        push_task = asyncio.create_task(server.push_proactive("surprise"))
        await asyncio.sleep(0.05)  # window where an unserialized push would interleave
        gate.set()
        await asyncio.gather(reply_task, push_task)
        return ws.frames

    frames = asyncio.run(_run())
    labels = [
        "wav:" + bytes(f).decode().split(":", 1)[1] if isinstance(f, (bytes, bytearray))
        else "json:" + json.loads(f)["type"]
        for f in frames
    ]
    assert labels == ["json:reply", "wav:the reply", "json:proactive_say", "wav:surprise"]


# --- end-to-end: due reminder -> notify -> thread bridge -> live client ---


def test_e2e_due_reminder_reaches_live_client_through_full_chain(tmp_path):
    """serve_forever running, real ReminderStore + Notifier, real loopback client:
    fire_due_reminders -> notify (worker thread) -> _push_from_thread bridge ->
    push_proactive -> proactive_say arrives on the client socket."""
    conn = db.connect(str(tmp_path / "jarvis.db"))
    reminder_store = ReminderStore(conn)
    notifier = Notifier(conn, quiet_hours="")
    server = JarvisServer(
        FakeAssistant(), notifier=notifier, reminder_store=reminder_store, port=0
    )

    async def _run():
        serve_task = asyncio.create_task(server.serve_forever())
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 2.0
        while server._ws_server is None:
            if loop.time() > deadline:
                raise TimeoutError("server never bound its socket")
            await asyncio.sleep(0.01)
        port = server._ws_server.sockets[0].getsockname()[1]
        try:
            async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
                await _wait_for_client_count(server, 1)
                # Added only after the client is connected so the startup tick
                # (which ran against an empty store) cannot consume it first.
                reminder_store.add("stretch", "2000-01-01T00:00:00")
                await server._fire_due_reminders()
                return json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
        finally:
            serve_task.cancel()
            try:
                await serve_task
            except asyncio.CancelledError:
                pass

    msg = asyncio.run(_run())
    assert msg == {"type": "proactive_say", "text": "Reminder: stretch", "audio_follows": False}
    # Delivered live to a client -> nothing fell back to the notice queue.
    assert notifier.drain() == []


# --- backward-compatible construction ---


def test_assistant_lock_holder_defaults_to_self():
    server = JarvisServer(FakeAssistant())
    assert server.assistant_lock_holder is server


def test_assistant_lock_holder_can_be_overridden():
    sentinel = object()
    server = JarvisServer(FakeAssistant(), assistant_lock_holder=sentinel)
    assert server.assistant_lock_holder is sentinel
