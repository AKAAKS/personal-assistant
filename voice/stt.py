"""Speech-to-text using faster-whisper (CTranslate2)."""

import logging
import os
import numpy as np

log = logging.getLogger(__name__)


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
        )
        return " ".join(seg.text.strip() for seg in segments).strip()
