import asyncio
import json

import numpy as np
import websockets

from jarvisd.core import INTERRUPTION_NOTE
from jarvisd.server import JarvisServer


class FakeAssistant:
    def __init__(self):
        self.was_reset = False
        # Barge-in capture (JarvisServer._capture_interrupt_target) reads this
        # directly, mirroring the real Assistant's conversation history.
        self.conversation = []

    def process(self, text):
        return f"echo: {text}"

    def reset_conversation(self):
        self.was_reset = True


class FakeWS:
    """Minimal stand-in for a websockets connection: just enough of a `.send`
    surface for _safe_send to write to, used by tests that drive
    JarvisServer's handlers directly (no real socket) for deterministic
    control over ordering between an interrupt frame and a later mutation of
    assistant.conversation."""

    def __init__(self):
        self.sent = []

    async def send(self, data):
        self.sent.append(data)


def run_client(server, scenario):
    """Start server on an ephemeral port, run `scenario(ws)` against it."""
    async def _run():
        async with websockets.serve(server.handle, "127.0.0.1", 0) as ws_server:
            port = ws_server.sockets[0].getsockname()[1]
            async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
                return await scenario(ws)
    return asyncio.run(_run())


def test_text_round_trip():
    async def scenario(ws):
        await ws.send(json.dumps({"type": "text_in", "text": "hi"}))
        return json.loads(await ws.recv())

    reply = run_client(JarvisServer(FakeAssistant()), scenario)
    assert reply == {"type": "reply", "text": "echo: hi", "heard": None, "audio_follows": False}


def test_reset():
    fake = FakeAssistant()

    async def scenario(ws):
        await ws.send(json.dumps({"type": "reset"}))
        return json.loads(await ws.recv())

    reply = run_client(JarvisServer(fake), scenario)
    assert reply["type"] == "reply"
    assert fake.was_reset


def test_bad_json_and_unknown_type():
    async def scenario(ws):
        await ws.send("{not json")
        first = json.loads(await ws.recv())
        await ws.send(json.dumps({"type": "warp_drive"}))
        second = json.loads(await ws.recv())
        return first, second

    first, second = run_client(JarvisServer(FakeAssistant()), scenario)
    assert first["type"] == "error"
    assert second["type"] == "error"


def test_audio_without_stt_is_an_error():
    async def scenario(ws):
        await ws.send(b"\x00\x00" * 100)
        return json.loads(await ws.recv())

    reply = run_client(JarvisServer(FakeAssistant()), scenario)
    assert reply["type"] == "error"


class FakeSTT:
    def transcribe(self, audio):
        assert audio.dtype == np.int16
        return "turn on the light"


class FakeTTS:
    def synthesize(self, text):
        return b"RIFF-fake-wav:" + text.encode()


def test_audio_round_trip_with_speech():
    async def scenario(ws):
        await ws.send(np.zeros(16000, dtype=np.int16).tobytes())
        reply = json.loads(await ws.recv())
        wav = await ws.recv()
        return reply, wav

    server = JarvisServer(FakeAssistant(), stt=FakeSTT(), tts=FakeTTS())
    reply, wav = run_client(server, scenario)
    assert reply["heard"] == "turn on the light"
    assert reply["text"] == "echo: turn on the light"
    assert reply["audio_follows"] is True
    assert isinstance(wav, (bytes, bytearray)) and wav.startswith(b"RIFF-fake-wav:")


def test_reply_markdown_is_stripped_before_send():
    class MarkdownAssistant(FakeAssistant):
        def process(self, text):
            return "**Sure** — here you go:\n- one\n- two 🎵"

    async def scenario(ws):
        await ws.send(json.dumps({"type": "text_in", "text": "hi"}))
        return json.loads(await ws.recv())

    reply = run_client(JarvisServer(MarkdownAssistant()), scenario)
    assert "**" not in reply["text"]
    assert "- one" not in reply["text"]
    assert "🎵" not in reply["text"]
    assert "Sure" in reply["text"] and "one" in reply["text"] and "two" in reply["text"]


def test_reply_markdown_is_stripped_before_synthesis():
    """The TTS must synthesize the cleaned prose, not the raw markdown."""
    class MarkdownAssistant(FakeAssistant):
        def process(self, text):
            return "**Sure** — here you go:\n- one\n- two 🎵"

    async def scenario(ws):
        await ws.send(json.dumps({"type": "text_in", "text": "hi", "speak": True}))
        reply = json.loads(await ws.recv())
        wav = await ws.recv()
        return reply, wav

    server = JarvisServer(MarkdownAssistant(), tts=FakeTTS())
    reply, wav = run_client(server, scenario)
    assert b"**" not in wav
    assert b"- one" not in wav
    assert "🎵".encode() not in wav
    assert wav == b"RIFF-fake-wav:" + reply["text"].encode()


def test_conversation_history_keeps_raw_markdown_untouched():
    """strip_markdown must clean the outgoing copy only -- never the model's
    own conversation history (what it reasons over on the next turn)."""
    raw = "**Sure** — here you go:\n- one\n- two 🎵"

    class MarkdownAssistant(FakeAssistant):
        def __init__(self):
            super().__init__()
            self.conversation = []

        def process(self, text):
            self.conversation.append({"role": "user", "content": text})
            self.conversation.append({"role": "assistant", "content": raw})
            return raw

    async def scenario(ws):
        await ws.send(json.dumps({"type": "text_in", "text": "hi"}))
        return json.loads(await ws.recv())

    assistant = MarkdownAssistant()
    reply = run_client(JarvisServer(assistant), scenario)
    assert "**" not in reply["text"]
    assert assistant.conversation[-1] == {"role": "assistant", "content": raw}


def test_silent_audio_returns_empty_reply():
    class SilentSTT:
        def transcribe(self, audio):
            return "   "

    async def scenario(ws):
        await ws.send(np.zeros(1600, dtype=np.int16).tobytes())
        return json.loads(await ws.recv())

    reply = run_client(JarvisServer(FakeAssistant(), stt=SilentSTT(), tts=FakeTTS()), scenario)
    assert reply["heard"] == "" and reply["audio_follows"] is False


class NoteAssistant(FakeAssistant):
    """Tracks note_interruption(turn) calls -- and which turn object each one
    targeted -- and -- critically -- whether each one happened strictly
    before the next process() call, to prove ordering."""

    def __init__(self):
        super().__init__()
        self.notes = 0
        self.call_order = []
        self.noted_turns = []

    def note_interruption(self, turn=None):
        self.notes += 1
        self.call_order.append("note_interruption")
        self.noted_turns.append(turn)

    def process(self, text):
        self.call_order.append("process")
        return super().process(text)


def test_interrupt_frame_marks_next_transcribed_turn():
    fake = NoteAssistant()
    fake.conversation = [{"role": "assistant", "content": "previous reply"}]

    async def scenario(ws):
        await ws.send(json.dumps({"type": "interrupt"}))
        await ws.send(np.zeros(16000, dtype=np.int16).tobytes())
        reply = json.loads(await ws.recv())
        if reply.get("audio_follows"):
            await ws.recv()  # drain the WAV frame
        return reply

    reply = run_client(JarvisServer(fake, stt=FakeSTT(), tts=FakeTTS()), scenario)
    assert fake.notes == 1
    assert reply["heard"] == "turn on the light"
    # Ordering matters: the model must see the cut-off marker before it reasons
    # about the new utterance, not after.
    assert fake.call_order == ["note_interruption", "process"]
    # The turn passed to note_interruption is the one captured at interrupt
    # RECEIPT (the only assistant turn present at that moment).
    assert fake.noted_turns == [{"role": "assistant", "content": "previous reply"}]


def test_interrupt_then_silence_marks_nothing():
    class SilentSTT:
        def transcribe(self, audio):
            return ""

    fake = NoteAssistant()
    fake.conversation = [{"role": "assistant", "content": "previous reply"}]

    async def scenario(ws):
        await ws.send(json.dumps({"type": "interrupt"}))
        await ws.send(np.zeros(1600, dtype=np.int16).tobytes())
        return json.loads(await ws.recv())

    run_client(JarvisServer(fake, stt=SilentSTT(), tts=FakeTTS()), scenario)
    assert fake.notes == 0
    assert fake.call_order == []  # process() never even ran for an empty transcript


def test_interrupt_without_following_audio_does_not_leak_across_connections():
    """An interrupt frame with no subsequent audio on that connection (e.g. the
    client disconnects, or a second connection comes in) must not mark some
    unrelated later turn -- the flag is per-connection, not global."""
    fake = NoteAssistant()
    server = JarvisServer(fake, stt=FakeSTT(), tts=FakeTTS())

    async def interrupt_then_disconnect(ws):
        await ws.send(json.dumps({"type": "interrupt"}))
        # Disconnect without ever sending audio on this connection.

    async def plain_audio_turn(ws):
        await ws.send(np.zeros(16000, dtype=np.int16).tobytes())
        reply = json.loads(await ws.recv())
        if reply.get("audio_follows"):
            await ws.recv()
        return reply

    run_client(server, interrupt_then_disconnect)
    reply = run_client(server, plain_audio_turn)

    assert fake.notes == 0
    assert reply["heard"] == "turn on the light"
    assert not server._pending_interrupt  # nothing left dangling after either connection


def test_pending_interrupt_stores_captured_turn_object():
    """self._pending_interrupt maps ws -> the captured turn dict (or None),
    not a bare flag: the interrupt branch must capture the target turn at
    RECEIPT, not merely remember that an interrupt happened."""
    fake = NoteAssistant()
    turn = {"role": "assistant", "content": "hi there"}
    fake.conversation = [turn]
    server = JarvisServer(fake)
    ws = FakeWS()

    asyncio.run(server._handle_json(ws, json.dumps({"type": "interrupt"})))

    assert server._pending_interrupt == {ws: turn}
    assert server._pending_interrupt[ws] is turn  # same object, not a copy


def test_pending_interrupt_stores_none_when_no_assistant_turn_yet():
    """If there's no assistant turn at all when the interrupt frame arrives
    (fresh conversation), the captured value is None -- not omitted, not a
    bare True flag."""
    fake = NoteAssistant()
    server = JarvisServer(fake)
    ws = FakeWS()

    asyncio.run(server._handle_json(ws, json.dumps({"type": "interrupt"})))

    assert server._pending_interrupt == {ws: None}


def test_late_barge_in_marks_turn_captured_at_receipt_not_newest():
    """Finding 1 (wrong-turn race): assistant.conversation is global and can
    gain new assistant turns (an HA-rule action via run_direct/run_smart, or
    another connected client's own exchange) in the window between the
    interrupt frame and the barge-in audio that consumes it. The turn that
    gets marked must be the one captured AT INTERRUPT RECEIPT, never whatever
    happens to be assistant.conversation's newest assistant turn once the
    audio actually arrives."""
    fake = NoteAssistant()
    interrupted_reply = {"role": "assistant", "content": "reply that was actually interrupted"}
    fake.conversation = [interrupted_reply]
    server = JarvisServer(fake, stt=FakeSTT(), tts=FakeTTS())
    ws = FakeWS()

    async def scenario():
        await server._handle_json(ws, json.dumps({"type": "interrupt"}))
        # A new assistant turn appears before the barge-in audio is
        # delivered -- this must NOT be the turn that gets marked.
        fake.conversation.append({"role": "assistant", "content": "unrelated later reply"})
        await server._handle_audio(ws, np.zeros(16000, dtype=np.int16).tobytes())

    asyncio.run(scenario())

    assert fake.notes == 1
    assert fake.noted_turns == [interrupted_reply]
    assert fake.conversation[0] is interrupted_reply
    assert fake.conversation[-1]["content"] == "unrelated later reply"  # untouched
    assert not server._pending_interrupt  # consumed


def test_stored_none_capture_skips_marking_but_still_processes():
    """Finding 2 (stranded flag) combined with the receipt-time capture fix:
    if there's no assistant turn to interrupt when the flag arrives, the
    captured target is None. Consuming that later must skip note_interruption
    entirely -- not walk the conversation and mark whatever happens to be its
    most recent assistant turn by then (that would reintroduce the wrong-turn
    bug via the no-arg fallback instead of the stale flag)."""
    fake = NoteAssistant()
    assert fake.conversation == []

    async def scenario(ws):
        await ws.send(json.dumps({"type": "interrupt"}))
        # No assistant turn exists yet when the interrupt is received.
        await ws.send(np.zeros(16000, dtype=np.int16).tobytes())
        reply = json.loads(await ws.recv())
        if reply.get("audio_follows"):
            await ws.recv()
        return reply

    reply = run_client(JarvisServer(fake, stt=FakeSTT(), tts=FakeTTS()), scenario)
    assert fake.notes == 0
    assert fake.call_order == ["process"]  # process still ran normally
    assert reply["heard"] == "turn on the light"
    assert reply["text"] == "echo: turn on the light"
