"""Speech-to-text using faster-whisper (CTranslate2)."""

import logging
import os
import re
import numpy as np

log = logging.getLogger(__name__)

# Phrases Whisper hallucinates from media bleed in the room — YouTube-style
# outros no one says to an assistant. A whole utterance matching one is noise,
# not a turn; rejecting it stops background audio from driving a hands-free loop.
_HALLUCINATION_PHRASES = (
    "see you in the next video",
    "see you in the next one",
    "see you next time",
    "thanks for watching",
    "thank you for watching",
    "dont forget to subscribe",
    "please subscribe",
    "like and subscribe",
    "subscribe to my channel",
    "thanks for listening",
)


def _is_hallucination(text):
    """True when the transcription is a stock Whisper hallucination (noise),
    not something a user would actually say to JARVIS."""
    norm = " ".join(re.sub(r"[^a-z0-9 ]+", " ", text.lower()).split())
    if not norm:
        return True
    if any(p in norm for p in _HALLUCINATION_PHRASES):
        return True
    tokens = norm.split()
    if all(t == "bye" for t in tokens):  # "bye", "bye bye bye"
        return True
    if norm == "you":  # Whisper's classic single-token invention on silence
        return True
    return False

# Confidence gates: Whisper hallucinates words on noise / the tail of JARVIS's
# own speech during the hands-free follow-up window. Drop any segment the model
# isn't confident is speech, so noise -> "" (no reply) instead of invented text.
NO_SPEECH_THRESHOLD = float(os.getenv("WHISPER_NO_SPEECH_THRESHOLD", "0.6"))
LOGPROB_THRESHOLD = float(os.getenv("WHISPER_LOGPROB_THRESHOLD", "-1.0"))


def _confident_text(segments, no_speech_threshold=NO_SPEECH_THRESHOLD,
                    logprob_threshold=LOGPROB_THRESHOLD):
    """Join only the segments Whisper is confident are real speech."""
    kept = []
    for seg in segments:
        if getattr(seg, "no_speech_prob", 0.0) > no_speech_threshold:
            continue
        if getattr(seg, "avg_logprob", 0.0) < logprob_threshold:
            continue
        text = seg.text.strip()
        if text:
            kept.append(text)
    return " ".join(kept).strip()


def _load_nvidia_dlls():
    """Add pip-installed NVIDIA DLL directories to the DLL search path."""
    try:
        import nvidia
        for pkg_dir in nvidia.__path__:
            bin_dir = os.path.join(pkg_dir, "cublas", "bin")
            if os.path.isdir(bin_dir):
                os.add_dll_directory(bin_dir)
            bin_dir = os.path.join(pkg_dir, "cudnn", "bin")
            if os.path.isdir(bin_dir):
                os.add_dll_directory(bin_dir)
    except ImportError:
        pass


class SpeechToText:
    """Transcribes audio using Whisper, GPU-accelerated when available."""

    def __init__(
        self,
        model_size: str = "small",
        device: str = "cuda",
        compute_type: str = "int8_float16",
    ):
        _load_nvidia_dlls()
        from faster_whisper import WhisperModel

        log.info(f"Loading Whisper '{model_size}' on {device} ({compute_type})...")
        try:
            self.model = WhisperModel(model_size, device=device, compute_type=compute_type)
        except Exception:
            log.warning("CUDA unavailable, falling back to CPU")
            self.model = WhisperModel(model_size, device="cpu", compute_type="int8")
        log.info("Whisper ready.")

    def transcribe(self, audio: np.ndarray) -> str:
        """Transcribe int16 16kHz audio to text."""
        audio_float = audio.astype(np.float32) / 32768.0
        segments, _ = self.model.transcribe(
            audio_float,
            language="en",
            beam_size=5,
            vad_filter=True,
            # Don't let a hallucinated segment seed the next one into more invented
            # text; each capture is a fresh, standalone utterance.
            condition_on_previous_text=False,
        )
        text = _confident_text(segments)
        return "" if _is_hallucination(text) else text
