"""Thin voice client: wake word + VAD locally; audio to jarvisd; plays replies.

Run: python -m clients.voice_client
"""

import asyncio
import logging
import os
import winsound

import numpy as np
import websockets
import websockets.exceptions

from clients.protocol import ReceiveLoop
from voice.audio_io import MicrophoneStream, VoiceActivityDetector
from voice.wake_word import WakeWordDetector

log = logging.getLogger(__name__)

WAKE_WORD_CHUNK = 1280  # openwakeword expects 80ms chunks at 16kHz


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
        """Block until wake word, then record until silence. PCM bytes or None."""
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
            self.mic.clear()  # drop the wake phrase, keep anything said from here on
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
                log.info("No speech after wake word")
                print("  (no speech detected)\n")
                self._failure_beep()
                return None
            log.info("Captured %.1fs of audio", len(audio) / 16000)
            return audio.tobytes()

    @staticmethod
    def _failure_beep():
        try:
            winsound.Beep(400, 250)
        except Exception:
            pass

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
                try:
                    while True:
                        pcm = await asyncio.to_thread(self._capture_utterance)
                        if pcm is None:
                            continue
                        await ws.send(pcm)
                        item = await receiver.get_reply(recv_task)
                        reply = item["data"]
                        if reply.get("type") == "error":
                            print(f"  [error] {reply.get('error')}\n")
                            self._failure_beep()
                            continue
                        if not reply.get("heard") and not reply.get("text"):
                            print("  (didn't catch that)\n")
                            self._failure_beep()
                            continue
                        if reply.get("heard"):
                            print(f"  You: {reply['heard']}")
                        if reply.get("text"):
                            print(f"  JARVIS: {reply['text']}\n")
                        if reply.get("audio_follows"):
                            wav = await item["wav_future"]
                            if wav is not None:
                                await self._play(wav)
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
