"""Voice pipeline orchestrator: wake word -> listen -> transcribe -> process -> speak."""

import logging
import os
import winsound

import numpy as np

from voice.audio_io import MicrophoneStream, VoiceActivityDetector
from voice.wake_word import WakeWordDetector
from voice.stt import SpeechToText
from voice.tts import TextToSpeech

log = logging.getLogger(__name__)


class VoicePipeline:
    """Always-listening voice assistant with wake word activation."""

    WAKE_WORD_CHUNK = 1280  # openwakeword expects 80ms chunks at 16kHz

    def __init__(self, assistant, config: dict | None = None):
        config = config or {}
        self.assistant = assistant

        print("Loading voice components...")

        self.mic = MicrophoneStream()
        self.vad = VoiceActivityDetector(
            aggressiveness=config.get("vad_aggressiveness", 2),
        )
        self.wake_word = WakeWordDetector(
            model_name=config.get("wake_word_model", "hey_jarvis_v0.1"),
            threshold=config.get("wake_word_threshold", 0.5),
        )
        self.stt = SpeechToText(
            model_size=config.get("whisper_model", "small"),
            device=config.get("whisper_device", "cuda"),
            compute_type=config.get("whisper_compute_type", "int8_float16"),
        )
        self.tts = TextToSpeech(
            rate=config.get("tts_rate", 180),
        )
        self._wake_buf = np.array([], dtype=np.int16)

        print("Voice pipeline ready.\n")

    def _beep(self):
        try:
            winsound.Beep(800, 200)
        except Exception:
            pass

    def run(self):
        """Main loop: listen for wake word, process commands, repeat."""
        print("JARVIS voice mode active. Listening for wake word...")
        print("Press Ctrl+C to exit.\n")

        self.mic.start()
        try:
            while True:
                self._process_one_cycle()
        except KeyboardInterrupt:
            print("\nShutting down...")
        finally:
            self.mic.stop()

    def _process_one_cycle(self):
        """One wake-listen-process-speak cycle."""
        frame = self.mic.read_frame(timeout=1.0)
        if frame is None:
            return

        # Buffer mic frames into 1280-sample chunks for openwakeword
        self._wake_buf = np.concatenate([self._wake_buf, frame])
        if len(self._wake_buf) < self.WAKE_WORD_CHUNK:
            return
        wake_chunk = self._wake_buf[:self.WAKE_WORD_CHUNK]
        self._wake_buf = self._wake_buf[self.WAKE_WORD_CHUNK:]

        if not self.wake_word.process_frame(wake_chunk):
            return

        log.info("Wake word detected")
        self._beep()
        self.mic.clear()
        self._wake_buf = np.array([], dtype=np.int16)

        audio = self.vad.record_until_silence(
            self.mic,
            max_seconds=15.0,
            silence_ms=800,
            speech_timeout_sec=5.0,
        )

        if audio is None or len(audio) < 1600:
            log.info("No speech after wake word")
            return

        text = self.stt.transcribe(audio)
        if not text.strip():
            log.info("Empty transcription")
            return

        print(f"  You: {text}")

        try:
            response = self.assistant.process(text)
            print(f"  JARVIS: {response}\n")
            self.tts.speak(response)
        except Exception as e:
            log.error(f"Processing error: {e}")
            self.tts.speak("Sorry, I encountered an error.")
