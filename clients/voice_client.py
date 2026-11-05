"""Thin voice client: wake word + VAD locally; audio to jarvisd; plays replies.

Run: python -m clients.voice_client
"""

import asyncio
import collections
import io
import json
import logging
import os
import sys
import tempfile
import time
import wave
import winsound

import numpy as np
import websockets
import websockets.exceptions

from clients.protocol import ReceiveLoop
from voice.audio_io import MicrophoneStream, VoiceActivityDetector
from voice.wake_word import WakeWordDetector

log = logging.getLogger(__name__)

WAKE_WORD_CHUNK = 1280  # openwakeword expects 80ms chunks at 16kHz

# After JARVIS replies, keep listening this many seconds for a follow-up WITHOUT
# requiring the wake word again, so a back-and-forth conversation is hands-free.
# If no speech starts within the window, we drop back to wake-word gating.
FOLLOWUP_WINDOW_SEC = float(os.getenv("FOLLOWUP_WINDOW_SEC", "6"))

# End the hands-free conversation after this many consecutive non-productive
# follow-ups (a silent window OR audio that transcribed to nothing). Without a
# cap, a noisy room keeps tripping the VAD -> empty transcription -> retry,
# looping forever; this bounds it so we re-arm the wake word.
MAX_FOLLOWUP_MISFIRES = int(os.getenv("MAX_FOLLOWUP_MISFIRES", "2"))

# How many utterances the server can reject as "not the enrolled speaker"
# before we give up on the conversation. These are almost always JARVIS's own
# TTS echo, so they must NOT count as misfires -- being told "didn't catch
# that" and dropped out of the conversation because the assistant heard itself
# is the bug. But a room with a TV in it must still terminate eventually.
MAX_UNVERIFIED = int(os.getenv("MAX_UNVERIFIED", "4"))

# --- Bluetooth output latency -----------------------------------------------
# winsound gives no playback-completion callback, so the end of a reply is
# inferred from the WAV header. That clock starts when PlaySound RETURNS, not
# when sound leaves the speaker: a Bluetooth A2DP path buffers 150-500ms. Two
# things went wrong because of it, and both are fixed by accounting for it:
#   1. SND_PURGE fired while audio was still queued, so JARVIS cut itself off
#      mid-sentence.
#   2. The delayed tail arrived after mic.clear(), inside the follow-up VAD
#      window, and was captured as a user turn -- the feedback loop.
# Measure yours with: python scripts/voice_calibrate.py
OUTPUT_LATENCY_SEC = float(os.getenv("OUTPUT_LATENCY_MS", "250")) / 1000.0
# Safety margin past the computed end of playout, for codec/jitter slop.
WATCH_TAIL_SEC = float(os.getenv("WATCH_TAIL_MS", "150")) / 1000.0
# Mic frames are read and thrown away for this long after a reply finishes, so
# any echo still in flight never reaches the follow-up VAD.
ECHO_DRAIN_SEC = float(os.getenv("ECHO_DRAIN_MS", "250")) / 1000.0

# Barge-in policy while a reply is playing (see PlaybackWakePolicy).
#
# These default to the SAME score and streak as idle listening on purpose. The
# first version demanded 0.7 over two consecutive chunks, which made barge-in
# strictly harder than waking JARVIS from idle and stopped it firing at all.
# That strictness was guarding against the wake model tripping on JARVIS's own
# speech -- a hypothesis no log ever supported, and one that is no longer
# load-bearing: a self-fire now just purges, records the echo, and has it
# rejected as `unverified` without ending the conversation.
#
# The level gate is the one genuinely physical discriminator, so it stays: a
# candidate quieter than the echo it is competing with cannot be someone
# talking over the speaker. Set PLAYBACK_LEVEL_RATIO=0 to disable even that.
PLAYBACK_WAKE_THRESHOLD = float(os.getenv("WAKE_WORD_THRESHOLD_PLAYBACK",
                                          os.getenv("WAKE_WORD_THRESHOLD", "0.5")))
PLAYBACK_WAKE_CONSECUTIVE = int(os.getenv("WAKE_WORD_CONSECUTIVE_PLAYBACK", "1"))

# OFF by default. Measured (logs/voice_client.log 15:17): the gate rejected
# EVERY genuine barge-in attempt, at scores of 0.97-1.00. A score that high is
# unambiguous, the self-fire the gate guarded against was never once observed in
# any log, and a self-fire is now harmless -- it purges, records the echo, and
# has it dropped as `unverified` without ending the conversation. So the gate
# costs real functionality to insure against a hypothetical.
# Set to ~1.2 to re-enable it; it now measures over the right window (below).
PLAYBACK_LEVEL_RATIO = float(os.getenv("PLAYBACK_LEVEL_RATIO", "0"))
PLAYBACK_MIN_LEVEL = float(os.getenv("PLAYBACK_MIN_LEVEL", "200"))

# openwakeword's score covers roughly the last 1.5s of audio, not the 80ms
# chunk it is returned for, so the level it gets compared against has to span
# the same window. 1.5s / 80ms per chunk.
RECEPTIVE_FIELD_CHUNKS = int(os.getenv("PLAYBACK_LEVEL_WINDOW_CHUNKS", "19"))

# Log any chunk that scored at least this much but did NOT barge in, with the
# gate that stopped it. Without this there was no way to tell a missing
# detection from a rejected one.
PLAYBACK_DIAG_SCORE = float(os.getenv("PLAYBACK_DIAG_SCORE", "0.3"))


class PlaybackWakePolicy:
    """Decides whether a wake-word hit during playback is really the user.

    While a reply plays, the mic is also hearing JARVIS off the room speaker,
    so the idle threshold is the wrong bar -- the old code fed its own TTS
    through the detector for the whole reply and trusted that it could never
    sound like "hey jarvis".

    Three discriminators are available, but only the first is ON by default --
    see the constants above for the measured reason, and change the env knobs
    there to re-enable the others:
      * the score threshold (active; defaults to the same value as idle
        listening, because demanding more made interrupting JARVIS harder than
        waking it);
      * the loudest chunk within the score's own ~1.5s window must beat the
        running echo floor, learned from the non-candidate chunks -- which
        during playback *are* the echo (OFF by default:
        PLAYBACK_LEVEL_RATIO=0);
      * the hit must repeat over consecutive chunks (effectively off:
        PLAYBACK_WAKE_CONSECUTIVE=1).

    The state behind the inactive gates is still maintained, because the
    near-miss diagnostic log reads it -- that log is what revealed the gate was
    rejecting every genuine barge-in.

    Known limit: for the first few hundred milliseconds of a reply the echo
    floor has not been learned yet, so the level gate rests on `min_level`
    alone and only the score and streak gates are fully effective. That window
    is also the one where the echo has not physically arrived (output latency)
    and the user has not yet heard anything to interrupt, so in practice there
    is little to catch there.

    Pure arithmetic, so the policy is unit-tested without openwakeword.
    """

    #  Smoothing for the echo-floor EMA. Low enough to settle within a word or
    #  two of playback starting, high enough not to chase individual syllables.
    ALPHA = 0.1

    def __init__(self, *, threshold: float = PLAYBACK_WAKE_THRESHOLD,
                 consecutive: int = PLAYBACK_WAKE_CONSECUTIVE,
                 level_ratio: float = PLAYBACK_LEVEL_RATIO,
                 min_level: float = PLAYBACK_MIN_LEVEL,
                 window_chunks: int = RECEPTIVE_FIELD_CHUNKS):
        self.threshold = threshold
        self.consecutive = max(1, consecutive)
        self.level_ratio = level_ratio
        self.min_level = min_level
        self.echo_floor = 0.0
        self.streak = 0
        self.last_reason = "idle"
        self.recent_levels = collections.deque(maxlen=max(1, window_chunks))

    def reset(self):
        self.echo_floor = 0.0
        self.streak = 0
        self.last_reason = "idle"
        self.recent_levels.clear()

    @property
    def candidate_level(self) -> float:
        """Loudest chunk within the score's receptive field.

        The peak, not the latest chunk: the wake phrase itself is the loud part
        and the detection fires on the quiet pause just after it.
        """
        return max(self.recent_levels, default=0.0)

    @property
    def required_level(self) -> float:
        """The level a candidate has to beat right now. 0.0 = gate disabled."""
        if self.level_ratio <= 0:
            return 0.0
        return max(self.echo_floor * self.level_ratio, self.min_level)

    def observe(self, *, score: float, level: float) -> bool:
        """Feed one chunk's wake-word score and RMS level. True = barge in.

        `last_reason` records which gate decided, for the diagnostic log.
        """
        self.recent_levels.append(level)
        if score < self.threshold:
            # Not a candidate, so this chunk is a sample of the echo itself.
            self.echo_floor += self.ALPHA * (level - self.echo_floor)
            self.streak = 0
            self.last_reason = "low_score"
            return False
        # A candidate: never let it raise the floor, or a long sentence would
        # gradually gate itself out.
        if self.candidate_level < self.required_level:
            self.streak = 0
            self.last_reason = "below_echo_floor"
            return False
        self.streak += 1
        if self.streak >= self.consecutive:
            self.streak = 0
            self.last_reason = "fire"
            return True
        self.last_reason = "need_more_chunks"
        return False


def rms(chunk: np.ndarray) -> float:
    """RMS level of an int16 chunk, in int16 units (float math, no overflow)."""
    if len(chunk) == 0:
        return 0.0
    return float(np.sqrt(np.mean(chunk.astype(np.float64) ** 2)))


class VoiceClient:

    def __init__(self, url: str):
        self.url = url
        print("Loading wake-word model...")
        self.mic = MicrophoneStream()
        self.vad = VoiceActivityDetector(aggressiveness=int(os.getenv("VAD_AGGRESSIVENESS", "2")))
        try:
            self.wake_word = WakeWordDetector(
                model_name=os.getenv("WAKE_WORD_MODEL", "hey_jarvis_v0.1"),
                threshold=float(os.getenv("WAKE_WORD_THRESHOLD", "0.5")),
            )
        except Exception as e:
            log.error("Wake-word model unavailable: %s", e)
            print(f"Wake-word model unavailable: {e}")
            raise SystemExit(1)
        self._wake_buf = np.array([], dtype=np.int16)
        self._playback_policy = PlaybackWakePolicy()
        # Serializes winsound.PlaySound calls: a proactive WAV must never
        # interleave with a reply WAV (winsound.PlaySound is blocking).
        self._play_lock = asyncio.Lock()

    def _capture_utterance(self) -> bytes | None:
        """Block until the wake word, then record one utterance. PCM or None."""
        while True:
            frame = self.mic.read_frame(timeout=1.0)
            if frame is None:
                continue
            self._wake_buf = np.concatenate([self._wake_buf, frame])
            if len(self._wake_buf) < WAKE_WORD_CHUNK:
                continue
            chunk = self._wake_buf[:WAKE_WORD_CHUNK]
            self._wake_buf = self._wake_buf[WAKE_WORD_CHUNK:]
            if not self.wake_word.process_frame(chunk):
                continue
            log.info("Wake word detected")
            return self._record_utterance()

    def _record_utterance(self) -> bytes | None:
        """Record one utterance NOW (no wake word): capture until silence.

        Returns PCM bytes, or None if nothing was said. Used both right after a
        wake-word detection and right after a barge-in interrupt.
        """
        self.mic.clear()  # drop the wake phrase / anything said while JARVIS talked
        self._wake_buf = np.array([], dtype=np.int16)
        print("  Listening...")
        audio = self.vad.record_until_silence(
            self.mic, max_seconds=15.0, silence_ms=800, speech_timeout_sec=5.0,
        )
        if audio is None:
            log.info("No speech captured")
            print("  (no speech detected)\n")
            return None  # the run loop beeps once when it re-arms the wake word
        log.info("Captured %.1fs of audio", len(audio) / 16000)
        return audio.tobytes()

    def _watch_for_wake(self, duration: float) -> bool:
        """While a reply plays, listen for a barge-in. Returns True if the user
        cut in, False if the clip finished playing with no interruption.

        The watch runs for the whole PHYSICAL playout -- clip length plus the
        output latency -- because the caller purges playback when this returns.
        Ending early is what used to chop the tail off a reply on Bluetooth.

        Echo safety is PlaybackWakePolicy's job, not an assumption that JARVIS
        never says anything resembling the wake word.
        """
        self.mic.clear()
        self._wake_buf = np.array([], dtype=np.int16)
        self._playback_policy.reset()
        deadline = time.monotonic() + duration + OUTPUT_LATENCY_SEC + WATCH_TAIL_SEC
        while time.monotonic() < deadline:
            frame = self.mic.read_frame(timeout=0.2)
            if frame is None:
                continue
            self._wake_buf = np.concatenate([self._wake_buf, frame])
            while len(self._wake_buf) >= WAKE_WORD_CHUNK:
                chunk = self._wake_buf[:WAKE_WORD_CHUNK]
                self._wake_buf = self._wake_buf[WAKE_WORD_CHUNK:]
                score = self.wake_word.score(chunk)
                level = rms(chunk)
                policy = self._playback_policy
                if policy.observe(score=score, level=level):
                    log.info(
                        "Barge-in accepted: score %.2f, chunk level %.0f, "
                        "window peak %.0f, echo floor %.0f",
                        score, level, policy.candidate_level, policy.echo_floor,
                    )
                    self.wake_word.reset()
                    return True
                if score >= PLAYBACK_DIAG_SCORE:
                    # A near miss. Says exactly which gate stopped it, so a
                    # barge-in that never fires can be told apart from one the
                    # policy rejected -- and tuned from numbers, not guesses.
                    log.info(
                        "Barge-in rejected [%s]: score %.2f (need %.2f), "
                        "chunk level %.0f, window peak %.0f (need %.0f), "
                        "echo floor %.0f, streak %d/%d",
                        policy.last_reason, score, policy.threshold,
                        level, policy.candidate_level, policy.required_level,
                        policy.echo_floor, policy.streak, policy.consecutive,
                    )
        return False

    def _drain_mic(self, seconds: float = ECHO_DRAIN_SEC):
        """Read and discard mic frames for `seconds`, then empty the queue.

        mic.clear() only drops what is ALREADY queued. The Bluetooth-delayed
        tail of JARVIS's own reply arrives after that instant, which is what the
        follow-up VAD was latching onto and shipping to the server as a user
        turn. Draining for a latency-sized window consumes it instead.
        """
        if seconds <= 0:
            return
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.mic.read_frame(timeout=0.05)
        self.mic.clear()
        self._wake_buf = np.array([], dtype=np.int16)

    def _play_and_watch(self, wav: bytes) -> bool:
        """Play a reply WAV asynchronously while watching for a barge-in.
        Returns True if the user interrupted, False if it finished on its own.

        winsound cannot play asynchronously from a memory buffer -- it raises
        "Cannot play asynchronously from memory" -- so async playback (the only
        kind that lets the wake-word watch run concurrently) goes through a temp
        file with SND_FILENAME | SND_ASYNC. The file is purged and removed once
        the watch ends.
        """
        duration = self._wav_duration(wav)
        if duration <= 0:
            # We cannot tell when this clip ends, so async playback would leave
            # it sounding while the next capture opens -- recording JARVIS as
            # the user's follow-up, the exact feedback loop this client exists
            # to avoid. (Purging instead would truncate the reply, which is how
            # an earlier version of this guard got it wrong in the other
            # direction.) Blocking playback returns only when the sound has
            # finished, so there is nothing left to purge or to bleed. The cost
            # is no barge-in for one malformed clip.
            try:
                winsound.PlaySound(wav, winsound.SND_MEMORY)
            except Exception:
                log.warning("Could not play a reply with an unreadable duration",
                            exc_info=True)
            self._drain_mic()
            return False
        tmp_path = None
        try:
            fd, tmp_path = tempfile.mkstemp(suffix=".wav", prefix="jarvis_reply_")
            os.close(fd)
            with open(tmp_path, "wb") as f:
                f.write(wav)
            winsound.PlaySound(tmp_path, winsound.SND_FILENAME | winsound.SND_ASYNC)
        except Exception:
            # Async playback unavailable: fall back to a plain blocking play from
            # memory (no barge-in this time, but the reply is still heard).
            try:
                winsound.PlaySound(wav, winsound.SND_MEMORY)
            except Exception:
                pass
            self._remove_tempfile(tmp_path)
            return False
        interrupted = False
        try:
            interrupted = self._watch_for_wake(duration)
        finally:
            # Unconditional: duration > 0 is guaranteed by the guard above, so
            # the watch either ran the full playout or the user cut in. Both
            # mean stopping the device is safe.
            try:
                winsound.PlaySound(None, winsound.SND_PURGE)
            except Exception:
                pass
            self._remove_tempfile(tmp_path)
        if not interrupted:
            self._drain_mic()  # swallow the Bluetooth-delayed tail
        return interrupted

    @staticmethod
    def _remove_tempfile(path: str | None) -> None:
        """Best-effort delete of a reply temp file; a stray clip never matters."""
        if not path:
            return
        try:
            os.unlink(path)
        except OSError:
            pass

    async def _play_and_watch_reply(self, wav: bytes) -> bool:
        """Play a reply under the shared play-lock; return whether interrupted."""
        async with self._play_lock:
            return await asyncio.to_thread(self._play_and_watch, wav)

    def _capture_followup(self) -> bytes | None:
        """Record a follow-up utterance WITHOUT the wake word.

        Used right after JARVIS replies so a conversation can continue
        hands-free. Returns PCM bytes if the user starts speaking within
        FOLLOWUP_WINDOW_SEC, or None if the window passes in silence (meaning
        the conversation is over and we should re-arm the wake word).
        """
        self.mic.clear()  # discard whatever was captured while JARVIS was talking
        audio = self.vad.record_until_silence(
            self.mic, max_seconds=15.0, silence_ms=800,
            speech_timeout_sec=FOLLOWUP_WINDOW_SEC,
        )
        if audio is None:
            return None
        log.info("Captured %.1fs follow-up", len(audio) / 16000)
        return audio.tobytes()

    _STOPPED_SOUND = os.path.join(os.path.dirname(__file__), "sounds", "stopped_listening.wav")

    @staticmethod
    def _stopped_beep():
        """The one and only cue: JARVIS has stopped listening and re-armed the
        wake word. No cue means it heard you and is thinking (a reply follows).
        A soft mallet note; falls back to a plain beep if the clip is missing."""
        try:
            winsound.PlaySound(VoiceClient._STOPPED_SOUND, winsound.SND_FILENAME)
        except Exception:
            try:
                winsound.Beep(400, 250)
            except Exception:
                pass

    @staticmethod
    def _wav_duration(wav: bytes) -> float:
        """Length of a WAV clip in seconds from its header (0.0 if unreadable)."""
        try:
            with wave.open(io.BytesIO(wav), "rb") as w:
                rate = w.getframerate()
                return w.getnframes() / float(rate) if rate else 0.0
        except (wave.Error, EOFError):
            return 0.0

    @staticmethod
    def _followup_next(*, in_conversation: bool, misfires: int, productive: bool):
        """Advance the hands-free conversation state after one exchange.

        `productive` is True when the server returned a real reply, False when
        the follow-up transcribed to nothing (a "misfire"). Returns the next
        `(in_conversation, misfires)`. A productive exchange keeps us in the
        conversation and clears the misfire streak; while in a conversation,
        consecutive misfires accumulate until MAX_FOLLOWUP_MISFIRES, at which
        point we leave conversation mode and re-arm the wake word. This is the
        guard against a noisy room looping forever on empty transcriptions.
        """
        if productive:
            return True, 0
        if in_conversation:
            misfires += 1
            if misfires >= MAX_FOLLOWUP_MISFIRES:
                return False, 0
        return in_conversation, misfires

    async def _play(self, wav: bytes):
        """Play a WAV under the shared lock so a proactive and a reply WAV
        can never sound simultaneously (winsound.PlaySound is blocking)."""
        async with self._play_lock:
            await asyncio.to_thread(winsound.PlaySound, bytes(wav), winsound.SND_MEMORY)
            # A proactive announcement is spoken while the main loop may be
            # sitting in a capture, so its echo needs draining too.
            await asyncio.to_thread(self._drain_mic)

    @staticmethod
    def _on_proactive_text(text: str):
        print(f"JARVIS (proactive): {text}")

    async def run(self):
        self.mic.start()
        print("Voice client active. Listening for wake word... Ctrl+C to exit.\n")
        try:
            async with websockets.connect(self.url, max_size=32 * 1024 * 1024) as ws:
                # One background receiver owns the socket read so a proactive
                # push can be spoken at any time -- including while the wake-word
                # capture loop below is blocked waiting for the next utterance.
                receiver = ReceiveLoop(
                    ws,
                    on_proactive_text=self._on_proactive_text,
                    on_proactive_audio=self._play,
                )
                recv_task = asyncio.create_task(receiver.run())
                in_conversation = False  # True during a hands-free back-and-forth
                misfires = 0             # consecutive non-productive follow-ups
                unverified = 0           # consecutive "not the enrolled speaker"
                pending_barge_in = False  # user interrupted; record their new turn next
                active = False           # woken and engaged, vs waiting for wake word

                def advance(productive: bool):
                    """Apply the follow-up state transition."""
                    nonlocal in_conversation, misfires
                    in_conversation, misfires = self._followup_next(
                        in_conversation=in_conversation, misfires=misfires,
                        productive=productive,
                    )

                def rearm():
                    """Back to wake-word-gated idle: beep ONCE so the user knows
                    JARVIS stopped listening (no beep = it's thinking)."""
                    nonlocal active
                    if active:
                        print("  (stopped listening — say 'hey jarvis' to start again)\n")
                        self._stopped_beep()
                        active = False

                try:
                    while True:
                        if pending_barge_in:
                            # We just cut off a reply; the user is already talking.
                            # pending_barge_in is only set from inside the
                            # playback watch, so a reply WAS sounding and an
                            # exchange is underway -- say so, or an unverified
                            # capture (JARVIS's own echo, which is the whole
                            # reason that branch exists) would see
                            # `not in_conversation` on the first exchange and
                            # rearm, ending the session.
                            pending_barge_in = False
                            in_conversation = True
                            pcm = await asyncio.to_thread(self._record_utterance)
                            if pcm is None:
                                advance(productive=False)
                                if not in_conversation:
                                    rearm()
                                continue
                        elif in_conversation:
                            print("  Listening for follow-up... (or stay quiet to end)")
                            pcm = await asyncio.to_thread(self._capture_followup)
                            if pcm is None:
                                misfires = MAX_FOLLOWUP_MISFIRES  # silent window ends it
                                advance(productive=False)
                                if not in_conversation:
                                    rearm()
                                continue
                        else:
                            pcm = await asyncio.to_thread(self._capture_utterance)
                            active = True  # the wake word fired
                            if pcm is None:
                                rearm()
                                continue
                        await ws.send(pcm)
                        item = await receiver.get_reply(recv_task)
                        reply = item["data"]
                        if reply.get("type") == "error":
                            print(f"  [error] {reply.get('error')}\n")
                            misfires = MAX_FOLLOWUP_MISFIRES
                            advance(productive=False)
                            if not in_conversation:
                                rearm()
                            continue
                        if reply.get("reason") == "unverified":
                            # Almost certainly JARVIS's own voice coming back off
                            # the speaker. Stay quiet and keep listening -- do NOT
                            # tell the user we didn't catch them, and do not burn
                            # a misfire, or hearing itself would end the
                            # conversation. A noisy room still terminates via
                            # MAX_UNVERIFIED.
                            unverified += 1
                            log.info("Ignored utterance %d/%d: not the enrolled "
                                     "speaker (likely TTS echo or another voice)",
                                     unverified, MAX_UNVERIFIED)
                            if not in_conversation or unverified >= MAX_UNVERIFIED:
                                unverified = 0
                                in_conversation, misfires = False, 0
                                rearm()
                            continue
                        unverified = 0
                        if not reply.get("heard") and not reply.get("text"):
                            print("  (didn't catch that)\n")
                            advance(productive=False)
                            if not in_conversation:
                                rearm()
                            continue
                        if reply.get("heard"):
                            print(f"  You: {reply['heard']}")
                        if reply.get("text"):
                            print(f"  JARVIS: {reply['text']}\n")
                        if reply.get("audio_follows"):
                            wav = await item["wav_future"]
                            if wav is not None:
                                interrupted = await self._play_and_watch_reply(wav)
                                if interrupted:
                                    print("  [interrupted]")
                                    await ws.send(json.dumps({"type": "interrupt"}))
                                    pending_barge_in = True
                                    continue  # skip advance; go record the new turn
                        # A completed exchange -> keep listening for a follow-up.
                        advance(productive=True)
                finally:
                    recv_task.cancel()
                    try:
                        await recv_task
                    except (asyncio.CancelledError, websockets.exceptions.ConnectionClosed):
                        pass
        finally:
            self.mic.stop()


def _setup_logging():
    """Log to logs/voice_client.log AND the console, mirroring the daemon.

    Previously this was console-only, and the file of that name existed solely
    because the tray redirects the child's stdout into it. So running the client
    from a terminal -- the normal way to test it -- threw every wake-word and
    barge-in diagnostic away with the scrollback, which is exactly the data
    needed to tune the barge-in gate. Rotating, so an always-on client can't
    grow it without bound.
    """
    from logging.handlers import RotatingFileHandler

    handlers: list[logging.Handler] = []
    try:
        os.makedirs("logs", exist_ok=True)
        handlers.append(RotatingFileHandler(
            os.path.join("logs", "voice_client.log"),
            maxBytes=2_000_000, backupCount=3, encoding="utf-8",
        ))
    except OSError:
        pass  # unwritable logs dir must not stop the client from running
    if sys.stderr is not None:  # no console under pythonw
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
    )


def main():
    _setup_logging()
    url = os.getenv("JARVIS_URL", "ws://127.0.0.1:8765")
    try:
        asyncio.run(VoiceClient(url).run())
    except KeyboardInterrupt:
        print("\nBye.")
    except websockets.exceptions.ConnectionClosed:
        print(f"Lost connection to jarvisd at {url}. Is it running? Start it with: python -m jarvisd")
    except (ConnectionRefusedError, OSError):
        print(f"Cannot reach jarvisd at {url}. Start it with: python -m jarvisd")


if __name__ == "__main__":
    main()
