"""Text-to-speech using pyttsx3 (Windows SAPI5).

Uses the built-in Windows speech engine for zero-latency offline TTS.
For higher quality voices, upgrade to edge-tts in a future phase.
"""

import logging
import pyttsx3

log = logging.getLogger(__name__)


class TextToSpeech:
    """Speaks text through the system TTS engine."""

    def __init__(self, rate: int = 180, voice_id: str | None = None):
        self.engine = pyttsx3.init()
        self.engine.setProperty("rate", rate)

        voices = self.engine.getProperty("voices")
        if voice_id:
            self.engine.setProperty("voice", voice_id)
        elif voices:
            self.engine.setProperty("voice", voices[0].id)

        log.info(f"TTS ready (rate={rate}, voices={len(voices or [])})")

    def speak(self, text: str) -> None:
        """Speak text aloud. Blocks until finished."""
        if not text:
            return
        self.engine.say(text)
        self.engine.runAndWait()

    def synthesize(self, text: str) -> bytes | None:
        """Render text to WAV bytes (SAPI5 writes RIFF WAV). None for empty text."""
        if not text:
            return None
        import os
        import tempfile
        fd, path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        try:
            self.engine.save_to_file(text, path)
            self.engine.runAndWait()
            with open(path, "rb") as f:
                return f.read()
        finally:
            os.unlink(path)

    def list_voices(self) -> list[dict]:
        """List available system voices."""
        voices = self.engine.getProperty("voices")
        return [{"id": v.id, "name": v.name, "languages": v.languages} for v in (voices or [])]
