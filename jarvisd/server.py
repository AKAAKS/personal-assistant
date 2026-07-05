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
    ):
        self.assistant = assistant
        self.stt = stt
        self.tts = tts
        self.host = host
        self.port = port
        self.notifier = notifier
        self.reminder_store = reminder_store
        self._lock = asyncio.Lock()  # one conversation; serialize agent access
        # Who owns the "one conversation at a time" guard for guarded tool execution
        # (the HA event listener runs rule actions under this same lock). Defaults to
        # this server so existing single-process wiring needs no changes.
        self.assistant_lock_holder = assistant_lock_holder if assistant_lock_holder is not None else self
        self._clients: set = set()
        self._loop = None  # captured in serve_forever(); lets worker threads push safely
        self._reminder_task = None

    async def serve_forever(self):
        self._loop = asyncio.get_running_loop()
        if self.notifier is not None:
            self.notifier.push_to_clients = self._push_from_thread
        if self.reminder_store is not None and self.notifier is not None:
            self._reminder_task = asyncio.create_task(self._reminder_tick())
        try:
            async with websockets.serve(self.handle, self.host, self.port, max_size=MAX_MESSAGE_BYTES):
                log.info("jarvisd listening on ws://%s:%d", self.host, self.port)
                await asyncio.Future()
        finally:
            if self._reminder_task is not None:
                self._reminder_task.cancel()

    async def handle(self, ws):
        self._clients.add(ws)
        try:
            async for message in ws:
                try:
                    if isinstance(message, (bytes, bytearray)):
                        await self._handle_audio(ws, bytes(message))
                    else:
                        await self._handle_json(ws, message)
                except Exception as e:
                    log.exception("request failed")
                    await ws.send(json.dumps({"type": "error", "error": str(e)}))
        finally:
            self._clients.discard(ws)

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
            await ws.send(json.dumps({"type": "error", "error": "invalid JSON"}))
            return
        mtype = msg.get("type")
        if mtype == "text_in":
            reply = await self._process(msg.get("text", ""))
            reply = await self._append_drained_notices(reply)
            await self._send_reply(ws, reply, heard=None, speak=bool(msg.get("speak")))
        elif mtype == "reset":
            self.assistant.reset_conversation()
            await ws.send(json.dumps(
                {"type": "reply", "text": "Conversation cleared.", "heard": None, "audio_follows": False}
            ))
        else:
            await ws.send(json.dumps({"type": "error", "error": f"unknown message type: {mtype!r}"}))

    async def _handle_audio(self, ws, pcm: bytes):
        if self.stt is None:
            await ws.send(json.dumps({"type": "error", "error": "speech not enabled on this server"}))
            return
        import numpy as np
        audio = np.frombuffer(pcm, dtype=np.int16)
        log.info("Audio in: %.1fs (%d bytes)", len(audio) / 16000, len(pcm))
        heard = await asyncio.to_thread(self.stt.transcribe, audio)
        if not heard.strip():
            log.info("Transcription empty — no reply generated")
            await ws.send(json.dumps({"type": "reply", "text": "", "heard": "", "audio_follows": False}))
            return
        log.info("Heard: %s", heard)
        reply = await self._process(heard)
        reply = await self._append_drained_notices(reply)
        await self._send_reply(ws, reply, heard=heard, speak=True)

    async def _send_reply(self, ws, reply: str, heard, speak: bool):
        wav = None
        if speak and self.tts is not None and reply:
            wav = await asyncio.to_thread(self.tts.synthesize, reply)
        await ws.send(json.dumps(
            {"type": "reply", "text": reply, "heard": heard, "audio_follows": wav is not None}
        ))
        if wav is not None:
            await ws.send(wav)

    async def push_proactive(self, text: str) -> int:
        """Send an unsolicited proactive_say message to every connected client.

        Returns the number of clients successfully reached. Iterates a snapshot of
        the client set (list(self._clients)) so the registry can shrink mid-loop
        without disturbing iteration; any client whose send fails is dropped from
        the registry and does not prevent delivery to the rest.
        """
        wav = None
        if self.tts is not None:
            wav = await asyncio.to_thread(self.tts.synthesize, text)
        payload = json.dumps({"type": "proactive_say", "text": text, "audio_follows": wav is not None})
        reached = 0
        for ws in list(self._clients):
            try:
                await ws.send(payload)
                if wav is not None:
                    await ws.send(wav)
                reached += 1
            except Exception:
                log.warning("proactive push failed for a client; dropping it", exc_info=True)
                self._clients.discard(ws)
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
