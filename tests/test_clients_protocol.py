"""Loopback tests for clients.protocol.ReceiveLoop: the concurrent receive/dispatch
logic shared by the CLI and voice thin clients (Task 5).

A fake server (websockets.serve on port 0) drives the scenario from the brief:
reply -> unsolicited proactive_say -> reply, plus an unknown message type and a
WAV frame pair for both a proactive push and a reply. No network beyond loopback,
no sleeps.
"""

import asyncio
import json

import pytest
import websockets

from clients.protocol import ReceiveLoop


async def _serve(handler):
    """Start a loopback server running `handler(ws)`; return (ws_server, port)."""
    ws_server = await websockets.serve(handler, "127.0.0.1", 0)
    port = ws_server.sockets[0].getsockname()[1]
    return ws_server, port


def test_receives_replies_in_order_and_fires_proactive_handler():
    proactive_seen = []

    async def server_handler(ws):
        # 1) answer a text_in with a reply
        await ws.recv()
        await ws.send(json.dumps({"type": "reply", "text": "first", "heard": None, "audio_follows": False}))
        # 2) push an UNSOLICITED proactive_say between replies
        await ws.send(json.dumps({"type": "proactive_say", "text": "unsolicited push", "audio_follows": False}))
        # 3) answer a second reply
        await ws.recv()
        await ws.send(json.dumps({"type": "reply", "text": "second", "heard": None, "audio_follows": False}))

    async def _run():
        ws_server, port = await _serve(server_handler)
        try:
            async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
                receiver = ReceiveLoop(ws, on_proactive_text=proactive_seen.append)
                recv_task = asyncio.create_task(receiver.run())

                await ws.send(json.dumps({"type": "text_in", "text": "hi"}))
                item1 = await receiver.get_reply(recv_task)

                await ws.send(json.dumps({"type": "text_in", "text": "hi again"}))
                item2 = await receiver.get_reply(recv_task)

                recv_task.cancel()
                try:
                    await recv_task
                except (asyncio.CancelledError, websockets.exceptions.ConnectionClosed):
                    pass
                return item1, item2
        finally:
            ws_server.close()
            await ws_server.wait_closed()

    item1, item2 = asyncio.run(_run())
    assert item1["data"]["type"] == "reply"
    assert item1["data"]["text"] == "first"
    assert item2["data"]["type"] == "reply"
    assert item2["data"]["text"] == "second"
    # Proactive push arrived and was dispatched between the two replies.
    assert proactive_seen == ["unsolicited push"]


def test_unknown_message_type_is_ignored_without_crashing():
    async def server_handler(ws):
        await ws.recv()
        await ws.send(json.dumps({"type": "from_the_future", "whatever": 123}))
        await ws.send(json.dumps({"type": "reply", "text": "still works", "heard": None, "audio_follows": False}))

    async def _run():
        ws_server, port = await _serve(server_handler)
        try:
            async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
                receiver = ReceiveLoop(ws)
                recv_task = asyncio.create_task(receiver.run())

                await ws.send(json.dumps({"type": "text_in", "text": "hi"}))
                item = await receiver.get_reply(recv_task)

                recv_task.cancel()
                try:
                    await recv_task
                except (asyncio.CancelledError, websockets.exceptions.ConnectionClosed):
                    pass
                return item
        finally:
            ws_server.close()
            await ws_server.wait_closed()

    item = asyncio.run(_run())
    assert item["data"]["text"] == "still works"


def test_proactive_wav_routes_to_proactive_audio_callback():
    proactive_audio = []

    async def server_handler(ws):
        await ws.send(json.dumps({"type": "proactive_say", "text": "wake up", "audio_follows": True}))
        await ws.send(b"PROACTIVE-WAV-BYTES")

    async def _run():
        ws_server, port = await _serve(server_handler)
        try:
            async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
                async def on_audio(wav):
                    proactive_audio.append(wav)

                receiver = ReceiveLoop(ws, on_proactive_audio=on_audio)
                recv_task = asyncio.create_task(receiver.run())

                # Wait for the proactive audio to actually arrive using an event
                # fed by the callback, rather than sleeping.
                got = asyncio.Event()

                async def on_audio_and_signal(wav):
                    proactive_audio.append(wav)
                    got.set()

                receiver.on_proactive_audio = on_audio_and_signal
                await asyncio.wait_for(got.wait(), timeout=2)

                recv_task.cancel()
                try:
                    await recv_task
                except (asyncio.CancelledError, websockets.exceptions.ConnectionClosed):
                    pass
        finally:
            ws_server.close()
            await ws_server.wait_closed()

    asyncio.run(_run())
    assert proactive_audio == [b"PROACTIVE-WAV-BYTES"]


def test_reply_wav_attached_to_queued_items_wav_future():
    async def server_handler(ws):
        await ws.recv()
        await ws.send(json.dumps({"type": "reply", "text": "with audio", "heard": None, "audio_follows": True}))
        await ws.send(b"REPLY-WAV-BYTES")

    async def _run():
        ws_server, port = await _serve(server_handler)
        try:
            async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
                receiver = ReceiveLoop(ws)
                recv_task = asyncio.create_task(receiver.run())

                await ws.send(json.dumps({"type": "text_in", "text": "hi"}))
                item = await receiver.get_reply(recv_task)
                wav = await asyncio.wait_for(item["wav_future"], timeout=2)

                recv_task.cancel()
                try:
                    await recv_task
                except (asyncio.CancelledError, websockets.exceptions.ConnectionClosed):
                    pass
                return wav
        finally:
            ws_server.close()
            await ws_server.wait_closed()

    wav = asyncio.run(_run())
    assert wav == b"REPLY-WAV-BYTES"


def test_reply_without_audio_resolves_wav_future_to_none():
    async def server_handler(ws):
        await ws.recv()
        await ws.send(json.dumps({"type": "reply", "text": "no audio", "heard": None, "audio_follows": False}))

    async def _run():
        ws_server, port = await _serve(server_handler)
        try:
            async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
                receiver = ReceiveLoop(ws)
                recv_task = asyncio.create_task(receiver.run())

                await ws.send(json.dumps({"type": "text_in", "text": "hi"}))
                item = await receiver.get_reply(recv_task)
                wav = await asyncio.wait_for(item["wav_future"], timeout=2)

                recv_task.cancel()
                try:
                    await recv_task
                except (asyncio.CancelledError, websockets.exceptions.ConnectionClosed):
                    pass
                return wav
        finally:
            ws_server.close()
            await ws_server.wait_closed()

    wav = asyncio.run(_run())
    assert wav is None


def test_get_reply_reraises_connection_closed_when_server_disappears():
    async def server_handler(ws):
        await ws.recv()
        # Close without ever answering -- the client is left waiting for a reply.
        await ws.close()

    async def _run():
        ws_server, port = await _serve(server_handler)
        try:
            async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
                receiver = ReceiveLoop(ws)
                recv_task = asyncio.create_task(receiver.run())

                await ws.send(json.dumps({"type": "text_in", "text": "hi"}))
                with pytest.raises(websockets.exceptions.ConnectionClosed):
                    await receiver.get_reply(recv_task)
        finally:
            ws_server.close()
            await ws_server.wait_closed()

    asyncio.run(_run())
