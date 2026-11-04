"""Wake word detection using openwakeword.

This stays deliberately thin: it reports the model's raw score and nothing more.
The *policy* -- which threshold applies, how many consecutive hits are needed,
whether the audio is loud enough to be the user rather than the room speaker --
lives in the client, where it can be unit-tested without openwakeword.
"""

import logging
import numpy as np

log = logging.getLogger(__name__)


class WakeWordDetector:
    """Listens for a wake word in streaming audio chunks."""

    def __init__(self, model_name: str = "hey_jarvis_v0.1", threshold: float = 0.5):
        from openwakeword.model import Model
        import openwakeword

        try:
            openwakeword.utils.download_models()
        except Exception as e:
            log.warning(f"Could not download models (may already exist): {e}")

        try:
            self.model = Model(wakeword_models=[model_name], inference_framework="onnx")
        except Exception:
            log.warning(f"Model '{model_name}' not found, falling back to 'hey_mycroft_v0.1'")
            self.model = Model(wakeword_models=["hey_mycroft_v0.1"], inference_framework="onnx")

        self.threshold = threshold
        self.last_score = 0.0
        log.info(f"Wake word detector ready (threshold={threshold})")

    def score(self, frame: np.ndarray) -> float:
        """Feed an audio frame (int16, 16kHz) and return the best model score.

        No threshold, no reset -- for callers that apply their own policy (the
        barge-in watch needs a stricter one than idle listening, because during
        playback the mic is also hearing JARVIS).
        """
        prediction = self.model.predict(frame)
        self.last_score = max(prediction.values(), default=0.0)
        return self.last_score

    def process_frame(self, frame: np.ndarray) -> bool:
        """Feed an audio frame (int16, 16kHz). Returns True if wake word detected."""
        if self.score(frame) >= self.threshold:
            self.model.reset()
            return True
        return False

    def reset(self):
        self.model.reset()
