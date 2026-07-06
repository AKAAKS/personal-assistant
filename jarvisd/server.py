"""WebSocket server exposing the assistant to thin clients.

Protocol:
  client -> brain  JSON  {"type": "text_in", "text": str, "speak": bool?}
                   JSON  {"type": "reset"}
                   binary one utterance of 16 kHz mono int16 PCM
  brain -> client  JSON  {"type": "reply", "text": str, "heard": str|null,
                          "audio_follows": bool}
                   JSON  {"type": "error", "error": str}
                   JSON  {"type": "proactive_say", "text": str, "audio_follows": bool}
                          (unsolicited -- can arrive at ANY time, between or during
                          any client-initiated exchange; clients must tolerate
                          unknown message types for forward compatibility)
                   binary WAV bytes (only right after audio_follows=true, whether
                          that followed a "reply" or a "proactive_say")
"""

import asyncio
import json
import logging
from datetime import datetime

import websockets

log = logging.getLogger(__name__)

MAX_MESSAGE_BYTES = 32 * 1024 * 1024  # 15s of 16kHz int16 is ~0.5MB; generous cap
REMINDER_TICK_SEC = 15


class JarvisServer:

    def __init__(
        self,
        assistant,
        stt=None,
        tts=None,
        host="127.0.0.1",
        port=8765,
        notifier=None,
        reminder_store=None,
        assistant_lock_holder=None,
        ha_listener=None,
    ):
        self.assistant = assistant
        self.stt = stt
        self.tts = tts
        self.host = host
        self.port = port
        self.notifier = notifier
        self.reminder_store = reminder_store
        # Optional HAEventListener; may also be set as an attribute after construction
        # (the wiring in jarvisd/__main__.py needs `server._lock` to build its
        # run_direct/run_smart callables, so the listener is often created after the
        # server -- see __main__.py for why). started/cancelled in serve_forever()
        # exactly like _reminder_task.
        self.ha_listener = ha_listener
        self._ha_listener_task = None
        self._lock = asyncio.Lock()  # one conversation; serialize agent access
        # Reserved hook for a future multi-server topology that would share one
        # assistant/lock. Currently unused by the wiring: __main__'s HA-rule
        # callables serialize on `server._lock` directly to match _process (see
        # __main__.py). Defaults to this server; kept for API stability.
        self.assistant_lock_holder = assistant_lock_holder if assistant_lock_holder is not None else self
        # ws -> asyncio.Lock serializing writes to that connection: a JSON+WAV frame
        # pair must never be split by a concurrent proactive push (or vice versa).
        self._clients: dict = {}
        self._loop = None  # captured in serve_forever(); lets worker threads push safely
        self._reminder_task = None
        self._ws_server = None  # set while serve_forever() is running (tests read the bound port)

    async def serve_forever(self):
        self._loop = asyncio.get_running_loop()
        if self.notifier is not None:
            self.notifier.push_to_clients = self._push_from_thread
        if self.reminder_store is not None and self.notifier is not None:
            self._reminder_task = asyncio.create_task(self._reminder_tick())
        if self.ha_listener is not None:
            self._ha_listener_task = asyncio.create_task(self.ha_listener.run())
        try:
            async with websockets.serve(
                self.handle, self.host, self.port, max_size=MAX_MESSAGE_BYTES
            ) as ws_server:
                self._ws_server = ws_server
                log.info("jarvisd listening on ws://%s:%d", self.host, self.port)
                await asyncio.Future()
        finally:
            self._ws_server = None
            if self._reminder_task is not None:
                self._reminder_task.cancel()
                try:
                    await self._reminder_task
                except asyncio.CancelledError:
                    pass
            if self._ha_listener_task is not None:
                self._ha_listener_task.cancel()
                try:
                    await self._ha_listener_task
                except asyncio.CancelledError:
                    pass

    async def handle(self, ws):
        self._clients[ws] = asyncio.Lock()
        try:
            async for message in ws:
                try:
                    if isinstance(message, (bytes, bytearray)):
                        await self._handle_audio(ws, bytes(message))
                    else:
                        await self._handle_json(ws, message)
                except Exception as e:
                    log.exception("request failed")
                    await self._safe_send(ws, json.dumps({"type": "error", "error": str(e)}))
        finally:
            self._clients.pop(ws, None)

    async def _process(self, text: str) -> str:
        async with self._lock:
            return await asyncio.to_thread(self.assistant.process, text)

    async def _append_drained_notices(self, reply: str) -> str:
        """Append any queued proactive notices to a reply.

        Called only after the agent call in the text_in/audio paths (never for
        reset or error paths) so recall/persistence still see the raw reply.
        """
        if self.notifier is None:
            return reply
        notices = await asyncio.to_thread(self.notifier.drain)
        if notices:
            reply = reply + "\n\nWhile you were away:\n" + "\n".join("- " + n for n in notices)
        return reply

    async def _handle_json(self, ws, raw: str):
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            await self._safe_send(ws, json.dumps({"type": "error", "error": "invalid JSON"}))
            return
        mtype = msg.get("type")
        if mtype == "text_in":
            reply = await self._process(msg.get("text", ""))
            reply = await self._append_drained_notices(reply)
            await self._send_reply(ws, reply, heard=None, speak=bool(msg.get("speak")))
        elif mtype == "reset":
            self.assistant.reset_conversation()
            await self._safe_send(ws, json.dumps(
                {"type": "reply", "text": "Conversation cleared.", "heard": None, "audio_follows": False}
            ))
        else:
            await self._safe_send(
                ws, json.dumps({"type": "error", "error": f"unknown message type: {mtype!r}"})
            )

    async def _handle_audio(self, ws, pcm: bytes):
        if self.stt is None:
            await self._safe_send(
                ws, json.dumps({"type": "error", "error": "speech not enabled on this server"})
            )
            return
        import numpy as np
        audio = np.frombuffer(pcm, dtype=np.int16)
        log.info("Audio in: %.1fs (%d bytes)", len(audio) / 16000, len(pcm))
        heard = await asyncio.to_thread(self.stt.transcribe, audio)
        if not heard.strip():
            log.info("Transcription empty — no reply generated")
            await self._safe_send(
                ws, json.dumps({"type": "reply", "text": "", "heard": "", "audio_follows": False})
            )
            return
        log.info("Heard: %s", heard)
        reply = await self._process(heard)
        reply = await self._append_drained_notices(reply)
        await self._send_reply(ws, reply, heard=heard, speak=True)

    async def _send_reply(self, ws, reply: str, heard, speak: bool):
        wav = None
        if speak and self.tts is not None and reply:
            wav = await asyncio.to_thread(self.tts.synthesize, reply)
        # _safe_send holds this connection's write lock across the JSON+WAV pair:
        # ws.send() can suspend on flow-control drain mid-pair, and a concurrent
        # push_proactive() to the same socket must not interleave its frames here
        # (the client would attribute the wrong WAV to audio_follows).
        frame = json.dumps(
            {"type": "reply", "text": reply, "heard": heard, "audio_follows": wav is not None}
        )
        if wav is not None:
            await self._safe_send(ws, frame, wav)
        else:
            await self._safe_send(ws, frame)

    def _client_write_lock(self, ws) -> asyncio.Lock:
        """Per-connection write lock; a throwaway lock for unregistered sockets."""
        lock = self._clients.get(ws)
        return lock if lock is not None else asyncio.Lock()

    async def _safe_send(self, ws, *frames):
        """Send one or more frames to a socket atomically w.r.t. that connection.

        Every write to a client goes through here so no frame -- not a bare error
        or reply, not either half of a JSON+WAV pair -- can ever reach the wire
        outside the connection's write lock and interpose into another exchange's
        frames (which would corrupt audio_follows attribution on the client).
        """
        async with self._client_write_lock(ws):
            for f in frames:
                await ws.send(f)

    async def push_proactive(self, text: str) -> int:
        """Send an unsolicited proactive_say message to every connected client.

        Returns the number of clients successfully reached. Iterates a snapshot
        (list of the registry) so it can shrink mid-loop without disturbing
        iteration; any client whose send fails is dropped from the registry and
        does not prevent delivery to the rest. Each connection's JSON+WAV pair is
        sent under that connection's write lock so it cannot interleave with a
        concurrent reply pair on the same socket.
        """
        wav = None
        if self.tts is not None:
            wav = await asyncio.to_thread(self.tts.synthesize, text)
        payload = json.dumps({"type": "proactive_say", "text": text, "audio_follows": wav is not None})
        reached = 0
        for ws in list(self._clients):
            try:
                if wav is not None:
                    await self._safe_send(ws, payload, wav)
                else:
                    await self._safe_send(ws, payload)
                reached += 1
            except Exception:
                log.warning("proactive push failed for a client; dropping it", exc_info=True)
                self._clients.pop(ws, None)
        return reached

    def _push_from_thread(self, text: str) -> int:
        """Thread-safe bridge wired to Notifier.push_to_clients.

        The Notifier calls this from a worker thread: the reminder tick invokes
        notify() via asyncio.to_thread, and the HA event listener will do the same,
        so this is never invoked on the event-loop thread itself. That matters
        because run_coroutine_threadsafe(...).result() blocks the calling thread
        until the coroutine finishes running on self._loop -- if the calling thread
        WERE the loop thread, that loop could never make progress on the scheduled
        coroutine and .result() would deadlock forever. Keep every notify() call
        off the loop thread via to_thread; the None/not-running check below is
        just a second line of defense for a loop-not-started-yet race.
        """
        if self._loop is None or not self._loop.is_running():
            return 0
        future = asyncio.run_coroutine_threadsafe(self.push_proactive(text), self._loop)
        return future.result(timeout=10)

    async def _fire_due_reminders(self):
        """One scheduler tick: fire every currently-due reminder and advance it.

        notifier.notify() does sqlite I/O and possibly a blocking HA HTTP call, and
        mark_fired() does sqlite I/O -- both run via asyncio.to_thread so the event
        loop is never blocked by them.
        """
        due = self.reminder_store.due(datetime.now())
        for r in due:
            await asyncio.to_thread(self.notifier.notify, f"Reminder: {r['text']}", "high")
            await asyncio.to_thread(self.reminder_store.mark_fired, r["id"], datetime.now())

    async def _reminder_tick(self):
        """Background scheduler: check for due reminders every REMINDER_TICK_SEC.

        A bad reminder (or a store/notifier hiccup) must not kill the loop.
        """
        while True:
            try:
                await self._fire_due_reminders()
            except Exception:
                log.exception("reminder tick failed")
            await asyncio.sleep(REMINDER_TICK_SEC)
