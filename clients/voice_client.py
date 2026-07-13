"""Thin voice client: wake word + VAD locally; audio to jarvisd; plays replies.

Run: python -m clients.voice_client
"""

import asyncio
import io
import logging
import os
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
        """Record one utterance NOW (no wake word): beep, capture until silence.

        Returns PCM bytes, or None if nothing was said. Used both right after a
        wake-word detection and right after a barge-in interrupt.
        """
        self.mic.clear()  # drop the wake phrase / anything said while JARVIS talked
        self._wake_buf = np.array([], dtype=np.int16)
        try:
            winsound.Beep(800, 200)
        except Exception:
            pass
        print("  Listening...")
        audio = self.vad.record_until_silence(
            self.mic, max_seconds=15.0, silence_ms=800, speech_timeout_sec=5.0,
        )
        if audio is None or len(audio) < 1600:
            log.info("No speech captured")
            print("  (no speech detected)\n")
            self._failure_beep()
            return None
        log.info("Captured %.1fs of audio", len(audio) / 16000)
        return audio.tobytes()

    def _watch_for_wake(self, duration: float) -> bool:
        """While a reply plays, listen for the wake word. Returns True on a
        barge-in, False if `duration` (+ a short tail) elapses with no wake word.

        Echo-safe: the mic hears JARVIS's own TTS, but the wake-word model only
        trips on 'hey jarvis' -- which JARVIS never says -- so it can't self-fire.
        """
        self.mic.clear()
        self._wake_buf = np.array([], dtype=np.int16)
        deadline = time.monotonic() + duration + 0.3  # small tail past clip end
        while time.monotonic() < deadline:
            frame = self.mic.read_frame(timeout=0.2)
            if frame is None:
                continue
            self._wake_buf = np.concatenate([self._wake_buf, frame])
            while len(self._wake_buf) >= WAKE_WORD_CHUNK:
                chunk = self._wake_buf[:WAKE_WORD_CHUNK]
                self._wake_buf = self._wake_buf[WAKE_WORD_CHUNK:]
                if self.wake_word.process_frame(chunk):
                    return True
        return False

    def _play_and_watch(self, wav: bytes) -> bool:
        """Play a reply WAV asynchronously while watching for the wake word.
        Returns True if the user interrupted, False if it finished on its own.
        """
        duration = self._wav_duration(wav)
        self._play_buf = wav  # hold a reference alive for async playback
        try:
            winsound.PlaySound(wav, winsound.SND_MEMORY | winsound.SND_ASYNC)
        except Exception:
            # Async playback unavailable: fall back to a plain blocking play
            # (no barge-in this time, but the reply is still heard).
            try:
                winsound.PlaySound(wav, winsound.SND_MEMORY)
            except Exception:
                pass
            return False
        try:
            return self._watch_for_wake(duration)
        finally:
            try:
                winsound.PlaySound(None, winsound.SND_PURGE)  # stop if still playing
            except Exception:
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
        try:
            winsound.Beep(1000, 120)  # soft, higher cue: "still listening"
        except Exception:
            pass
        audio = self.vad.record_until_silence(
            self.mic, max_seconds=15.0, silence_ms=800,
            speech_timeout_sec=FOLLOWUP_WINDOW_SEC,
        )
        if audio is None or len(audio) < 1600:
            return None
        log.info("Captured %.1fs follow-up", len(audio) / 16000)
        return audio.tobytes()

    @staticmethod
    def _failure_beep():
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
                in_conversation = False  # True while a hands-free back-and-forth is active
                misfires = 0  # consecutive non-productive follow-ups this conversation

                def advance(productive: bool):
                    """Apply the state transition and announce a conversation end."""
                    nonlocal in_conversation, misfires
                    was = in_conversation
                    in_conversation, misfires = self._followup_next(
                        in_conversation=in_conversation, misfires=misfires,
                        productive=productive,
                    )
                    if was and not in_conversation:
                        print("  (conversation ended — say 'hey jarvis' to start again)\n")

                try:
                    while True:
                        if in_conversation:
                            print("  Listening for follow-up... (or stay quiet to end)")
                            pcm = await asyncio.to_thread(self._capture_followup)
                            if pcm is None:
                                # A fully silent window is a strong "done" signal
                                # (a quiet room) — end the conversation at once.
                                misfires = MAX_FOLLOWUP_MISFIRES
                                advance(productive=False)
                                continue
                        else:
                            pcm = await asyncio.to_thread(self._capture_utterance)
                            if pcm is None:
                                continue
                        await ws.send(pcm)
                        item = await receiver.get_reply(recv_task)
                        reply = item["data"]
                        if reply.get("type") == "error":
                            print(f"  [error] {reply.get('error')}\n")
                            self._failure_beep()
                            misfires = MAX_FOLLOWUP_MISFIRES  # force end on error
                            advance(productive=False)
                            continue
                        if not reply.get("heard") and not reply.get("text"):
                            print("  (didn't catch that)\n")
                            self._failure_beep()
                            # In a conversation this is usually the room's ambient
                            # noise tripping the VAD, not the user — accumulate
                            # toward ending rather than looping forever.
                            advance(productive=False)
                            continue
                        if reply.get("heard"):
                            print(f"  You: {reply['heard']}")
                        if reply.get("text"):
                            print(f"  JARVIS: {reply['text']}\n")
                        if reply.get("audio_follows"):
                            wav = await item["wav_future"]
                            if wav is not None:
                                await self._play(wav)
                        # A completed exchange -> keep listening for a follow-up
                        # without the wake word, and reset the misfire streak.
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
