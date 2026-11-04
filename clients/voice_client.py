"""Thin voice client: wake word + VAD locally; audio to jarvisd; plays replies.

Run: python -m clients.voice_client
"""

import asyncio
import io
import json
import logging
import os
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

# Shortest audio worth sending. The old floor was 1600 samples (0.1s), which is
# far below any real command and let echo blips through to the server.
MIN_UTTERANCE_SAMPLES = int(os.getenv("MIN_UTTERANCE_SAMPLES", "8000"))  # 0.5s

# Barge-in policy while a reply is playing (see PlaybackWakePolicy).
PLAYBACK_WAKE_THRESHOLD = float(os.getenv("WAKE_WORD_THRESHOLD_PLAYBACK", "0.7"))
PLAYBACK_WAKE_CONSECUTIVE = int(os.getenv("WAKE_WORD_CONSECUTIVE_PLAYBACK", "2"))
PLAYBACK_LEVEL_RATIO = float(os.getenv("PLAYBACK_LEVEL_RATIO", "2.0"))
PLAYBACK_MIN_LEVEL = float(os.getenv("PLAYBACK_MIN_LEVEL", "200"))


class PlaybackWakePolicy:
    """Decides whether a wake-word hit during playback is really the user.

    While a reply plays, the mic is also hearing JARVIS off the room speaker,
    so the idle threshold is the wrong bar -- the old code fed its own TTS
    through the detector for the whole reply and trusted that it could never
    sound like "hey jarvis".

    Three cheap discriminators, no echo canceller required:
      * a stricter score threshold than idle listening;
      * the chunk must be clearly louder than the running echo floor (you
        speaking near the mic beats the speaker's bleed), where the floor is
        learned from the non-candidate chunks -- which during playback *are*
        the echo;
      * the hit must repeat over consecutive chunks, so one fluke can't fire.

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
                 min_level: float = PLAYBACK_MIN_LEVEL):
        self.threshold = threshold
        self.consecutive = max(1, consecutive)
        self.level_ratio = level_ratio
        self.min_level = min_level
        self.echo_floor = 0.0
        self.streak = 0

    def reset(self):
        self.echo_floor = 0.0
        self.streak = 0

    def observe(self, *, score: float, level: float) -> bool:
        """Feed one chunk's wake-word score and RMS level. True = barge in."""
        if score < self.threshold:
            # Not a candidate, so this chunk is a sample of the echo itself.
            self.echo_floor += self.ALPHA * (level - self.echo_floor)
            self.streak = 0
            return False
        # A candidate: never let it raise the floor, or a long sentence would
        # gradually gate itself out.
        if level < max(self.echo_floor * self.level_ratio, self.min_level):
            self.streak = 0
            return False
        self.streak += 1
        if self.streak >= self.consecutive:
            self.streak = 0
            return True
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
        if audio is None or len(audio) < MIN_UTTERANCE_SAMPLES:
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
                if self._playback_policy.observe(score=score, level=rms(chunk)):
                    log.info(
                        "Barge-in accepted: score %.2f, level %.0f, echo floor %.0f",
                        score, rms(chunk), self._playback_policy.echo_floor,
                    )
                    self.wake_word.reset()
                    return True
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
            # Purge only when we know where the clip ends: the watch just ran
            # for its full playout, or the user cut in deliberately. With an
            # unreadable duration (0.0) we have no idea, so stopping would be a
            # guess that truncates the reply -- let it finish instead.
            if interrupted or duration > 0:
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
        if audio is None or len(audio) < MIN_UTTERANCE_SAMPLES:
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
                            pending_barge_in = False
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


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s", datefmt="%H:%M:%S")
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
