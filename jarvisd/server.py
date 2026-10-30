"""WebSocket server exposing the assistant to thin clients.

Protocol:
  client -> brain  JSON  {"type": "text_in", "text": str, "speak": bool?}
                   JSON  {"type": "reset"}
                   JSON  {"type": "interrupt"}
                          (barge-in: client cut off a spoken reply via the wake
                          word; carries no data. No reply frame is sent. The
                          server captures the assistant's most recent reply
                          turn RIGHT NOW, at receipt -- not later, when the
                          next binary utterance on this connection arrives --
                          because assistant.conversation is shared/global and
                          could otherwise gain unrelated turns (an HA-rule
                          action, another client) in the meantime. Once that
                          next utterance yields non-empty text, the captured
                          turn -- not whatever is newest by then -- is marked
                          cut off before the new turn is processed.)
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
import os
from datetime import datetime, timedelta

import websockets

from jarvisd.text_format import strip_markdown

log = logging.getLogger(__name__)

MAX_MESSAGE_BYTES = 32 * 1024 * 1024  # 15s of 16kHz int16 is ~0.5MB; generous cap
REMINDER_TICK_SEC = 15


class JarvisServer:

    def __init__(
        self,
        assistant,
        stt=None,
        tts=None,
        speaker_verifier=None,
        host="127.0.0.1",
        port=8765,
        notifier=None,
        reminder_store=None,
        assistant_lock_holder=None,
        ha_listener=None,
        meal_store=None,
        habit_scan=None,
        proposal_store=None,
    ):
        self.assistant = assistant
        self.stt = stt
        self.tts = tts
        # Optional SpeakerVerifier; when set, utterances that aren't the enrolled
        # voice are dropped before transcription (silently, like empty speech).
        self.speaker_verifier = speaker_verifier
        self.host = host
        self.port = port
        self.notifier = notifier
        self.reminder_store = reminder_store
        # Optional MealStore; used only to evaluate the "meal_not_logged_today"
        # standing-nudge condition in _fire_due_reminders. May be None (e.g. before
        # bootstrap wires it up) -- conditional reminders then fail open (fire anyway).
        self.meal_store = meal_store
        # Optional HAEventListener; may also be set as an attribute after construction
        # (the wiring in jarvisd/__main__.py needs `server._lock` to build its
        # run_direct/run_smart callables, so the listener is often created after the
        # server -- see __main__.py for why). started/cancelled in serve_forever()
        # exactly like _reminder_task.
        self.ha_listener = ha_listener
        self._ha_listener_task = None
        # Optional zero-arg callable (bootstrap closure around run_habit_scan) that
        # runs the nightly habit-mining pipeline and returns the count of newly
        # queued proposals. None disables the nightly job entirely (_habit_task is
        # then never started -- see serve_forever()).
        self.habit_scan = habit_scan
        self._habit_task = None
        # Optional ProposalStore; used only to surface a one-line "you have N
        # pending suggestions" trailer on replies (see _append_habit_trailer). May
        # be None (nightly scan not wired up yet), in which case no trailer is ever
        # appended.
        self.proposal_store = proposal_store
        self._lock = asyncio.Lock()  # one conversation; serialize agent access
        # Reserved hook for a future multi-server topology that would share one
        # assistant/lock. Currently unused by the wiring: __main__'s HA-rule
        # callables serialize on `server._lock` directly to match _process (see
        # __main__.py). Defaults to this server; kept for API stability.
        self.assistant_lock_holder = assistant_lock_holder if assistant_lock_holder is not None else self
        # ws -> asyncio.Lock serializing writes to that connection: a JSON+WAV frame
        # pair must never be split by a concurrent proactive push (or vice versa).
        self._clients: dict = {}
        # Per-connection barge-in target: ws -> the assistant turn dict
        # captured at {"type":"interrupt"} RECEIPT (or None if there was no
        # assistant turn yet), consumed by the next transcribed audio turn on
        # that connection to mark that captured turn cut off. Captured at
        # receipt time rather than at consume time (a bare flag) so a barge-in
        # that arrives much later -- or never, if the user says the wake word
        # and then nothing -- still marks the reply that was actually
        # interrupted instead of whatever assistant.conversation's newest
        # turn happens to be by then.
        self._pending_interrupt: dict = {}
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
        if self.habit_scan is not None:
            self._habit_task = asyncio.create_task(self._habit_tick())
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
            if self._habit_task is not None:
                self._habit_task.cancel()
                try:
                    await self._habit_task
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
            self._pending_interrupt.pop(ws, None)

    async def _process(self, text: str, interrupted_turn=None) -> str:
        async with self._lock:
            # A captured turn of None means either no interrupt happened, or
            # one did but there was no assistant turn to mark at receipt --
            # both cases mean "nothing was playing that could be cut off", so
            # note_interruption isn't called at all (calling it with turn=None
            # would instead trigger its no-arg walk-and-mark-most-recent
            # fallback, which is not what's wanted here).
            if interrupted_turn is not None:
                self.assistant.note_interruption(interrupted_turn)
            return await asyncio.to_thread(self.assistant.process, text)

    def _capture_interrupt_target(self):
        """Return the assistant's most recent assistant-role turn right now,
        or None if there isn't one.

        Called at {"type":"interrupt"} frame RECEIPT (see _handle_json), not
        at barge-in audio consume time, so a late (or indefinitely delayed)
        barge-in still marks the reply that was actually interrupted.
        assistant.conversation is shared/global -- an HA-rule action
        (__main__.py's run_direct/run_smart) or another connected client can
        append new turns in the window before this connection's next audio
        frame arrives, and capturing now (rather than a bare flag consumed
        later) is what keeps that window from mattering.
        """
        conversation = getattr(self.assistant, "conversation", None) or []
        for turn in reversed(conversation):
            if turn.get("role") == "assistant":
                return turn
        return None

    async def _append_drained_notices(self, reply: str) -> str:
        """Append any queued proactive notices to a reply.

        Called only after the agent call in the text_in/audio paths (never for
        reset or error paths) so recall/persistence still see the raw reply.
        """
        if self.notifier is None:
            return reply
        notices = await asyncio.to_thread(self.notifier.drain)
        if notices:
            joined = "; ".join(notices)
            reply = reply + f"\n\nAlso, while you were away: {joined}."
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
            reply = await self._append_habit_trailer(reply)
            await self._send_reply(ws, reply, heard=None, speak=bool(msg.get("speak")))
        elif mtype == "reset":
            self.assistant.reset_conversation()
            await self._safe_send(ws, json.dumps(
                {"type": "reply", "text": "Conversation cleared.", "heard": None, "audio_follows": False}
            ))
        elif mtype == "interrupt":
            # Client cut off a spoken reply via the wake word. Capture the
            # target turn NOW rather than record a bare flag: assistant.
            # conversation is shared/global, so waiting until the barge-in
            # audio's _process (which can arrive seconds later, or never)
            # risks marking a turn that was never spoken to this client if
            # something else appends to the conversation in between (see
            # _capture_interrupt_target). Captured under self._lock -- the
            # same lock guarding every other read/mutation of
            # assistant.conversation (_process, and __main__.py's
            # run_direct/run_smart) -- so the capture can't race a concurrent
            # append. No reply frame -- the client isn't awaiting one (it's
            # already recording).
            async with self._lock:
                turn = self._capture_interrupt_target()
            self._pending_interrupt[ws] = turn
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
        # Consume regardless of outcome: pop returns the turn captured at
        # interrupt receipt (or None -- either because there was no interrupt
        # flag at all, or there was one but no assistant turn existed to
        # capture; _process treats both identically, as "nothing to mark").
        interrupted_turn = self._pending_interrupt.pop(ws, None)
        import numpy as np
        audio = np.frombuffer(pcm, dtype=np.int16)
        log.info("Audio in: %.1fs (%d bytes)", len(audio) / 16000, len(pcm))
        # Reject anyone but the enrolled speaker before spending STT on it. This
        # also drops JARVIS's own TTS echoing back through a room speaker, which
        # is what otherwise drives a hands-free feedback loop.
        if self.speaker_verifier is not None:
            if not await asyncio.to_thread(self.speaker_verifier.accepts, audio):
                log.info("Utterance rejected — not the enrolled speaker")
                await self._safe_send(
                    ws, json.dumps({"type": "reply", "text": "", "heard": "", "audio_follows": False})
                )
                return
        heard = await asyncio.to_thread(self.stt.transcribe, audio)
        if not heard.strip():
            log.info("Transcription empty — no reply generated")
            await self._safe_send(
                ws, json.dumps({"type": "reply", "text": "", "heard": "", "audio_follows": False})
            )
            return
        log.info("Heard: %s", heard)
        reply = await self._process(heard, interrupted_turn=interrupted_turn)
        reply = await self._append_drained_notices(reply)
        reply = await self._append_habit_trailer(reply)
        await self._send_reply(ws, reply, heard=heard, speak=True)

    async def _send_reply(self, ws, reply: str, heard, speak: bool):
        reply = strip_markdown(reply)  # clean prose for both display and TTS
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

        notifier.notify() does sqlite I/O and possibly a blocking HA HTTP call,
        mark_fired() does sqlite I/O, and meal_store.logged_today() does sqlite I/O
        too -- all run via asyncio.to_thread so the event loop is never blocked by
        them.

        Conditional reminders (r["condition"] set): a standing nudge like
        "meal_not_logged_today" still advances recurrence via mark_fired every tick,
        but the notify is skipped when the condition says it's unnecessary (e.g. a
        meal was already logged today). An unrecognized condition value fails open
        -- it fires (with a warning logged) rather than silently going dark, since a
        typo in a condition string must never permanently suppress a reminder.
        """
        due = self.reminder_store.due(datetime.now())
        for r in due:
            if await self._should_skip_notify(r):
                log.info("Reminder #%s condition satisfied; skipping notify", r["id"])
            else:
                await asyncio.to_thread(self.notifier.notify, f"Reminder: {r['text']}", "high")
            await asyncio.to_thread(self.reminder_store.mark_fired, r["id"], datetime.now())

    async def _should_skip_notify(self, r) -> bool:
        """Whether a due reminder's condition says to skip notifying (but still fire/advance)."""
        condition = r["condition"]
        if condition is None:
            return False
        if condition == "meal_not_logged_today":
            if self.meal_store is None:
                return False  # fail-open: no meal_store wired up yet
            return await asyncio.to_thread(self.meal_store.logged_today)
        log.warning("Unknown reminder condition %r on reminder #%s; firing anyway", condition, r["id"])
        return False

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

    def _seconds_until_next_scan(self, now: datetime) -> float:
        """Seconds from `now` until the next HABIT_SCAN_HOUR (env, default 3am local).

        Factored out of _habit_tick so it's independently testable without
        waiting for a real multi-hour sleep: pass a fixed `now` and assert the
        computed delay directly.
        """
        # Parse-guard the env var: this runs OUTSIDE the scan try/except, so a
        # malformed HABIT_SCAN_HOUR ("3am", "25") raising here would silently
        # kill the nightly task while the rest of the daemon keeps running.
        try:
            hour = int(os.environ.get("HABIT_SCAN_HOUR", "3"))
        except (TypeError, ValueError):
            hour = 3
        if not 0 <= hour <= 23:
            hour = 3
        target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return (target - now).total_seconds()

    async def _habit_tick(self):
        """Background scheduler: run the nightly habit scan once a day.

        Mirrors _reminder_tick's shape exactly: do the work, then sleep. A bad
        scan (sqlite hiccup, phraser error, etc.) must not kill the loop --
        it's logged and the daemon carries on to try again next cycle.
        """
        while True:
            try:
                count = await asyncio.to_thread(self.habit_scan)
                log.info("habit scan complete: %d new proposal(s)", count)
            except Exception:
                log.exception("habit scan failed")
            await asyncio.sleep(self._seconds_until_next_scan(datetime.now()))

    async def _append_habit_trailer(self, reply: str) -> str:
        """Append a one-line nudge when there are pending habit proposals.

        Never dumps proposal text (the user pulls details via list_proposals /
        "show suggestions") and never fires when proposal_store is unset or has
        nothing pending. Composes with _append_drained_notices -- both trailers
        can appear on the same reply; neither clobbers the other.
        """
        if self.proposal_store is None:
            return reply
        pending = await asyncio.to_thread(self.proposal_store.list_pending)
        if pending:
            n = len(pending)
            suggestion = "suggestion" if n == 1 else "suggestions"
            reply = reply + f"\n\n\U0001F4A1 You have {n} habit {suggestion} — say 'show suggestions' to review."
        return reply
