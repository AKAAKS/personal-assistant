"""Wake word detection using openwakeword."""

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
        log.info(f"Wake word detector ready (threshold={threshold})")

    def process_frame(self, frame: np.ndarray) -> bool:
        """Feed an audio frame (int16, 16kHz). Returns True if wake word detected."""
        prediction = self.model.predict(frame)
        for score in prediction.values():
            if score >= self.threshold:
                self.model.reset()
                return True
        return False

    def reset(self):
        self.model.reset()
