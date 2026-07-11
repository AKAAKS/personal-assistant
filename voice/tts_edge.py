"""Text-to-speech using edge-tts (Microsoft Edge's free neural voices).

edge-tts is async and streams MP3 over the network (no API key, no cost).
The client/server protocol here is WAV-out, so we decode the MP3 to PCM with
`miniaudio` (a self-contained pip wheel with its own bundled decoders — no
system ffmpeg dependency) and wrap the PCM in a RIFF/WAVE header with the
stdlib `wave` module.

Mirrors the thread-isolation discipline in voice/tts.py: synthesis (network
call + decode) runs on its own daemon worker thread with its own asyncio
event loop, and a join timeout degrades a hung/failed synthesis to None so
callers fall back to a text-only reply instead of freezing.

edge_tts and miniaudio are imported LAZILY inside the worker thread so a
missing package degrades to None at call time rather than crashing at
module import time (e.g. when the default pyttsx3 backend is used and
these optional deps aren't installed).
"""

import asyncio
import io
import logging
import os
import threading
import wave

# Single source of truth for the synthesis timeout (voice.tts has no top-level
# dependency on this module — make_tts imports EdgeTTS lazily — so this import
# is cycle-free). Sharing it keeps the daemon-freeze guard from drifting between
# the two TTS backends.
from voice.tts import SYNTH_TIMEOUT_SEC

log = logging.getLogger(__name__)


def _miniaudio_decode(mp3_bytes: bytes, output_format=None, nchannels=None, sample_rate=None):
    """Thin wrapper around miniaudio.decode so tests can monkeypatch it."""
    import miniaudio
    if output_format is None:
        output_format = miniaudio.SampleFormat.SIGNED16
    return miniaudio.decode(
        mp3_bytes, output_format=output_format, nchannels=nchannels, sample_rate=sample_rate
    )


class EdgeTTS:
    """Renders text to speech through edge-tts's free neural voice endpoint."""

    def __init__(self, voice: str | None = None, rate: str | None = None):
        self.voice = voice or os.getenv("EDGE_TTS_VOICE", "en-US-GuyNeural")
        self.rate = rate

    def synthesize(self, text: str, timeout: float = SYNTH_TIMEOUT_SEC) -> bytes | None:
        """Render text to WAV bytes via edge-tts (MP3) -> miniaudio (PCM) -> wave (RIFF).

        Returns None for empty text, on any failure (network, missing
        package, decode error), or on timeout, so callers can degrade to a
        text-only reply.
        """
        if not text or not text.strip():
            return None
        result: dict = {}
        worker = threading.Thread(
            target=self._synth_worker, args=(text, result), daemon=True, name="edge-tts-synth"
        )
        worker.start()
        worker.join(timeout)
        if worker.is_alive():
            log.warning("edge-tts synthesis timed out after %.0fs; replying without audio", timeout)
            return None
        return result.get("wav")

    def _synth_worker(self, text: str, result: dict):
        try:
            mp3_bytes = self._generate_mp3(text, self.voice)
            dec = _miniaudio_decode(
                mp3_bytes,
                nchannels=1,
                sample_rate=24000,
            )
            out = io.BytesIO()
            with wave.open(out, "wb") as w:
                w.setnchannels(dec.nchannels)
                w.setsampwidth(2)
                w.setframerate(dec.sample_rate)
                w.writeframes(bytes(dec.samples))
            result["wav"] = out.getvalue()
        except Exception:
            log.warning("edge-tts synthesis failed; returning None", exc_info=True)

    def _generate_mp3(self, text: str, voice: str) -> bytes:
        """Run the async edge-tts stream-to-MP3-bytes call. Own event loop, own thread."""
        import edge_tts

        async def _gen():
            comm = edge_tts.Communicate(text, voice)
            buf = bytearray()
            async for chunk in comm.stream():
                if chunk["type"] == "audio":
                    buf.extend(chunk["data"])
            return bytes(buf)

        return asyncio.run(_gen())
