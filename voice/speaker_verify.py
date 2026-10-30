"""Speaker verification: JARVIS responds only to the enrolled voice.

A voice-embedding model (Resemblyzer) turns an utterance into a d-vector; we
compare it to the enrolled voiceprint by cosine similarity and accept only if
it clears a threshold. This rejects JARVIS's own TTS (a different voice bleeding
back through a room speaker) and random triggers from other people/the TV, in
one mechanism.

The embedding model is injected into SpeakerVerifier, so the accept/reject
decision is pure and unit-tested without the model or any audio. The real
collaborators (encoder load, audio embedding, voiceprint I/O) live at the bottom
and are exercised only live, like the STT model.
"""

import logging
import os

import numpy as np

log = logging.getLogger(__name__)

VOICEPRINT_PATH = os.path.join("data", "voiceprint.npy")


def cosine_similarity(a, b) -> float:
    a = np.asarray(a, dtype=np.float64)
    b = np.asarray(b, dtype=np.float64)
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def make_voiceprint(embeddings) -> np.ndarray:
    """Average several enrollment embeddings into one unit-length voiceprint."""
    embs = [np.asarray(e, dtype=np.float64) for e in embeddings]
    if not embs:
        raise ValueError("need at least one embedding to enroll")
    mean = np.mean(embs, axis=0)
    norm = np.linalg.norm(mean)
    return mean / norm if norm else mean


class SpeakerVerifier:
    """Decides whether an utterance is the enrolled speaker.

    Args:
        embed_fn: callable(audio int16 ndarray) -> embedding vector.
        voiceprint: the enrolled unit vector, or None to disable verification
            (not enrolled -> accept everything, so nobody is locked out).
        threshold: minimum cosine similarity to accept.
    """

    def __init__(self, *, embed_fn, voiceprint=None, threshold: float = 0.75):
        self._embed = embed_fn
        self.voiceprint = None if voiceprint is None else np.asarray(voiceprint, dtype=np.float64)
        self.threshold = threshold

    @property
    def enabled(self) -> bool:
        return self.voiceprint is not None

    def similarity(self, audio) -> float:
        return cosine_similarity(self._embed(audio), self.voiceprint)

    def accepts(self, audio) -> bool:
        """True if the utterance is the enrolled speaker (or verification off)."""
        if not self.enabled:
            return True
        try:
            sim = self.similarity(audio)
        except Exception:
            # Fail open: a verification error must never make JARVIS deaf.
            log.warning("speaker verify errored; accepting utterance", exc_info=True)
            return True
        ok = sim >= self.threshold
        log.info("speaker match %.3f (threshold %.2f) -> %s",
                 sim, self.threshold, "accept" if ok else "reject")
        return ok


# --- real collaborators (live only; not exercised by unit tests) ------------

def load_encoder():
    """Load the Resemblyzer voice encoder (uses CUDA when available)."""
    from resemblyzer import VoiceEncoder
    return VoiceEncoder()


def embed_audio(encoder, audio_int16: np.ndarray) -> np.ndarray:
    """Embed 16kHz mono int16 PCM into a 256-d d-vector."""
    from resemblyzer import preprocess_wav
    wav = audio_int16.astype(np.float32) / 32768.0
    wav = preprocess_wav(wav, source_sr=16000)
    return encoder.embed_utterance(wav)


def load_voiceprint(path: str = VOICEPRINT_PATH):
    return np.load(path) if os.path.exists(path) else None


def from_env():
    """Build a verifier if verification is on AND a voiceprint is enrolled;
    otherwise return None (the server then skips verification entirely)."""
    if os.getenv("SPEAKER_VERIFY", "1") == "0":
        return None
    voiceprint = load_voiceprint()
    if voiceprint is None:
        log.info("Speaker verification idle: no voiceprint enrolled "
                 "(run: python -m clients.enroll_voice)")
        return None
    encoder = load_encoder()
    threshold = float(os.getenv("SPEAKER_VERIFY_THRESHOLD", "0.75"))
    return SpeakerVerifier(
        embed_fn=lambda a: embed_audio(encoder, a),
        voiceprint=voiceprint,
        threshold=threshold,
    )
