"""Shared concurrent receive/dispatch logic for jarvisd thin clients.

Both clients (cli.py, voice_client.py) restructure from lockstep send->recv to
a single background receiver that owns the socket read, so an unsolicited
proactive_say can be handled the instant it arrives instead of waiting for the
next lockstep round-trip. ReceiveLoop is that receiver: it is the ONLY
coroutine that ever calls ws.recv() for a connection, and it dispatches every
incoming frame by type:

  - "reply" / "error" JSON -> queued for the waiting send side (see get_reply())
  - "proactive_say" JSON   -> on_proactive_text(text) invoked immediately
  - unknown JSON type      -> ignored (forward compatibility)
  - binary WAV             -> routed to whichever JSON frame most recently
                               claimed audio_follows=true: a proactive WAV goes
                               to on_proactive_audio(wav); a reply/error WAV is
                               attached to that queued item's `wav_future` for
                               the send side to await (or ignore entirely, as
                               the CLI does -- it never plays audio).

Protocol reference: jarvisd/server.py module docstring.
"""

import asyncio
import json


async def _maybe_await(result):
    """Callbacks may be sync or async; await only if the call returned a coroutine."""
    if asyncio.iscoroutine(result):
        await result


class ReceiveLoop:
    def __init__(self, ws, on_proactive_text=None, on_proactive_audio=None):
        self.ws = ws
        self.queue: asyncio.Queue = asyncio.Queue()
        self.on_proactive_text = on_proactive_text
        self.on_proactive_audio = on_proactive_audio
        # Which JSON frame most recently claimed audio_follows=true, i.e. who the
        # next binary frame belongs to: None | "reply" | "proactive".
        self._pending = None
        self._pending_item = None

    async def run(self):
        """Read every frame on this connection until it closes.

        Uses ws.recv() (not `async for`) so a closed connection raises
        ConnectionClosed here exactly as the old lockstep code's ws.recv() calls
        did. The `finally` always pushes a None sentinel so a send side blocked
        in get_reply() is woken up and can re-raise via `await recv_task`.
        """
        try:
            while True:
                message = await self.ws.recv()
                if isinstance(message, (bytes, bytearray)):
                    await self._handle_binary(bytes(message))
                else:
                    await self._handle_text(message)
        finally:
            await self.queue.put(None)

    async def get_reply(self, recv_task):
        """Await the next reply/error item.

        Returns {"data": <parsed json>, "wav_future": asyncio.Future}. If the
        connection closes while waiting, re-raises whatever exception ended
        `recv_task` (mirroring the old ws.recv()-raises-on-close behavior).
        """
        item = await self.queue.get()
        if item is None:
            await recv_task  # re-raises the receive loop's exception, if any
            return None
        return item

    async def _handle_text(self, raw):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            self._pending = None
            self._pending_item = None
            return
        mtype = data.get("type")
        if mtype in ("reply", "error"):
            wav_future = asyncio.get_running_loop().create_future()
            item = {"data": data, "wav_future": wav_future}
            if data.get("audio_follows"):
                self._pending = "reply"
                self._pending_item = item
            else:
                wav_future.set_result(None)
                self._pending = None
                self._pending_item = None
            await self.queue.put(item)
        elif mtype == "proactive_say":
            if self.on_proactive_text is not None:
                await _maybe_await(self.on_proactive_text(data.get("text", "")))
            self._pending = "proactive" if data.get("audio_follows") else None
            self._pending_item = None
        else:
            # Unknown message type: ignore without crashing (forward compat).
            self._pending = None
            self._pending_item = None

    async def _handle_binary(self, wav: bytes):
        if self._pending == "reply" and self._pending_item is not None:
            self._pending_item["wav_future"].set_result(wav)
        elif self._pending == "proactive" and self.on_proactive_audio is not None:
            await _maybe_await(self.on_proactive_audio(wav))
        self._pending = None
        self._pending_item = None
