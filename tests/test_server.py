import asyncio
import json

import websockets

from jarvisd.server import JarvisServer


class FakeAssistant:
    def __init__(self):
        self.was_reset = False

    def process(self, text):
        return f"echo: {text}"

    def reset_conversation(self):
        self.was_reset = True


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
