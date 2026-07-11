"""Text-to-speech using pyttsx3 (Windows SAPI5).

SAPI5 engines are apartment-threaded COM objects: reusing one engine across
threads (asyncio.to_thread rotates pool threads) deadlocks the second
runAndWait() with no exception. So every synthesis runs on its own thread
with its own engine, and a join timeout degrades a hung synthesis to a
text-only reply instead of freezing the daemon.

For higher quality voices, upgrade to edge-tts in a future phase.
"""

import logging
import os
import tempfile
import threading

log = logging.getLogger(__name__)

SYNTH_TIMEOUT_SEC = 20


def _new_engine(rate: int, voice_id: str | None):
    try:
        import comtypes
        comtypes.CoInitialize()
    except Exception:
        pass
    from pyttsx3.engine import Engine  # bypass pyttsx3.init()'s shared-engine cache
    engine = Engine()
    engine.setProperty("rate", rate)
    if voice_id:
        engine.setProperty("voice", voice_id)
    return engine


class TextToSpeech:
    """Renders text to speech through the system TTS engine."""

    def __init__(self, rate: int = 180, voice_id: str | None = None):
        self.rate = rate
        self.voice_id = voice_id
        engine = _new_engine(rate, voice_id)  # fail fast; resolve the default voice
        voices = engine.getProperty("voices")
        if not voice_id and voices:
            self.voice_id = voices[0].id
        log.info(f"TTS ready (rate={rate}, voices={len(voices or [])})")
        self._engine_factory = lambda: _new_engine(self.rate, self.voice_id)

    def synthesize(self, text: str, timeout: float = SYNTH_TIMEOUT_SEC) -> bytes | None:
        """Render text to WAV bytes (SAPI5 writes RIFF WAV).

        Returns None for empty text, on synthesis failure, or on timeout,
        so callers can degrade to a text-only reply.
        """
        if not text:
            return None
        result: dict = {}
        worker = threading.Thread(
            target=self._synth_worker, args=(text, result), daemon=True, name="tts-synth"
        )
        worker.start()
        worker.join(timeout)
        if worker.is_alive():
            log.warning("TTS synthesis timed out after %.0fs; replying without audio", timeout)
            return None
        return result.get("wav")

    def _synth_worker(self, text: str, result: dict):
        try:
            fd, path = tempfile.mkstemp(suffix=".wav")
            os.close(fd)
            try:
                engine = self._engine_factory()
                engine.save_to_file(text, path)
                engine.runAndWait()
                with open(path, "rb") as f:
                    result["wav"] = f.read()
            finally:
                try:
                    os.unlink(path)
                except OSError:
                    pass
        except Exception:
            log.warning("TTS synthesis failed; returning None", exc_info=True)

    def list_voices(self) -> list[dict]:
        """List available system voices."""
        voices = self._engine_factory().getProperty("voices")
        return [{"id": v.id, "name": v.name, "languages": v.languages} for v in (voices or [])]


class _EdgeWithFallback:
    """Wraps EdgeTTS so a None result (network down, missing package, timeout)
    falls back to pyttsx3's TextToSpeech instead of degrading straight to text.
    The pyttsx3 engine is constructed lazily, only if/when it's actually needed."""

    def __init__(self, edge, rate: int, voice_id: str | None):
        self._edge = edge
        self._rate = rate
        self._voice_id = voice_id
        self._fallback = None

    def synthesize(self, text: str, timeout: float = SYNTH_TIMEOUT_SEC) -> bytes | None:
        wav = self._edge.synthesize(text, timeout)
        if wav is not None:
            return wav
        if self._fallback is None:
            self._fallback = TextToSpeech(rate=self._rate, voice_id=self._voice_id)
        return self._fallback.synthesize(text, timeout)


def make_tts(backend: str | None = None, rate: int = 180, voice_id: str | None = None):
    """Factory selecting a TTS backend by name (or TTS_BACKEND env var).

    "edge" -> EdgeTTS (free Microsoft neural voices, network required),
    wrapped with an automatic fallback to the offline pyttsx3 TextToSpeech
    if edge synthesis ever returns None. Anything else (including the
    default) -> the current, known-good, offline TextToSpeech.

    `rate` and `voice_id` are pyttsx3 (SAPI) settings — they apply to the
    plain pyttsx3 backend and to the edge backend's pyttsx3 fallback. They are
    intentionally NOT forwarded to EdgeTTS: edge voices are named differently
    (e.g. "en-US-GuyNeural", set via EDGE_TTS_VOICE) and take a percentage-string
    rate, so passing a SAPI voice GUID would silently break every edge synthesis
    and force a permanent fallback. The edge backend reads EDGE_TTS_VOICE itself.
    """
    if backend is None:
        backend = os.getenv("TTS_BACKEND", "pyttsx3")
    if backend == "edge":
        from voice.tts_edge import EdgeTTS
        edge = EdgeTTS()  # reads EDGE_TTS_VOICE; NOT the SAPI voice_id
        return _EdgeWithFallback(edge, rate=rate, voice_id=voice_id)
    return TextToSpeech(rate=rate, voice_id=voice_id)
