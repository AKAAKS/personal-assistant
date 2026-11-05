"""Speaker verification: JARVIS responds only to the enrolled voice.

A voice-embedding model turns an utterance into a speaker vector; we compare it
to the enrolled voiceprint by cosine similarity. This rejects random triggers
from other people or the TV, and -- critically -- JARVIS's own TTS bleeding back
into the mic from a room speaker, which is what otherwise drives a hands-free
feedback loop.

Why the decision is relative, not just a threshold
--------------------------------------------------
A single absolute threshold could not do that job. Live logs showed the
enrolled speaker scoring 0.666-0.868 and the TTS echo scoring 0.375-0.630, with
the threshold (0.75) sitting *inside* the speaker's own band: the same sentence
accepted at 0.751 and rejected at 0.748. Raising it deafens JARVIS, lowering it
admits the echo.

So we also keep a NEGATIVE print of JARVIS's own TTS voice and accept only an
utterance that is both good enough in absolute terms AND clearly closer to the
enrolled speaker than to JARVIS. That comparison stays valid however far the
absolute scores drift with mic, distance or room.

The embedding model is injected into SpeakerVerifier, so the accept/reject
decision is pure and unit-tested without the model or any audio. The real
collaborators (encoder load, audio embedding, print I/O) live at the bottom and
are exercised only live, like the STT model.
"""

import json
import logging
import os
from typing import NamedTuple

import numpy as np

log = logging.getLogger(__name__)

VOICEPRINT_PATH = os.path.join("data", "voiceprint.npy")
IMPOSTOR_PATH = os.path.join("data", "impostor_jarvis.npy")
CONFIG_PATH = os.path.join("data", "verify_config.json")

# Embedding width per backend, used to refuse a voiceprint enrolled with the
# other model -- comparing a 256-d Resemblyzer print against a 192-d ECAPA
# embedding would otherwise produce garbage scores and reject everything.
BACKEND_DIMS = {"ecapa": 192, "resemblyzer": 256}
DEFAULT_BACKEND = "ecapa"

# Below this much audio the embedding is too noisy to trust in absolute terms
# (it is why short commands scored 0.37-0.62 while the same voice scored
# 0.70-0.87 on a 3s clip). We still trust the *relative* comparison, so a short
# clip is decided by the margin rule alone.
DEFAULT_MIN_DURATION_SEC = 2.0

# Cosine scores are NOT comparable across embedding models, so the defaults are
# per backend. Getting this wrong is the original bug in a new costume: 0.75 is
# an ordinary same-speaker score for Resemblyzer and sits near the TOP of
# ECAPA's measured genuine band (0.290-0.775 on this hardware), so reusing it
# rejected almost everything. The ECAPA numbers are the midpoints of the bands
# measured live and pinned in tests/test_verify_live_scores.py, which also
# asserts these defaults accept every genuine row and reject every mixed one.
BACKEND_DEFAULT_THRESHOLD = {"ecapa": 0.25, "resemblyzer": 0.75}
BACKEND_DEFAULT_MARGIN = {"ecapa": 0.24, "resemblyzer": 0.10}

# Kept for callers that construct SpeakerVerifier directly; from_env() resolves
# per backend instead. These track the default backend.
DEFAULT_MARGIN = BACKEND_DEFAULT_MARGIN["ecapa"]
DEFAULT_THRESHOLD = BACKEND_DEFAULT_THRESHOLD["ecapa"]


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


# Bounds for a derived threshold. Below MIN nothing is really being verified;
# above MAX a normal utterance could never clear it.
#
# MIN is 0.20 because ECAPA's cosine scale is NOT the d-vector scale these were
# first written for: same-speaker lands ~0.3-0.8 and different-speaker sits at
# or below zero, where Resemblyzer put same-speaker at 0.7-0.9. A 0.40 floor
# clamped a correctly-derived 0.24 upward and then rejected genuine 1-2s
# utterances scoring 0.29. Measured from live logs: genuine speech >= 0.290,
# JARVIS-mixed audio <= 0.242.
THRESHOLD_MIN = 0.20
THRESHOLD_MAX = 0.95
# Where in the gap between the impostor band and the user band to place the
# threshold. Below 0.5 biases toward the impostor side, i.e. toward accepting
# the user -- deliberate, because the margin rule is the real guard against the
# echo and a false reject is the symptom we are fixing.
GAP_BIAS = 0.4
# Keep the required margin well inside the worst gap actually observed.
# Enrollment phrases are spoken deliberately and score a much wider gap than
# real commands do: measured min gap was ~0.60 at enrollment but 0.285 in use,
# so half the enrollment gap was still too strict.
MARGIN_HEADROOM = 0.33
MARGIN_MIN = 0.05
MARGIN_MAX = 0.40


def calibrate(*, user_sims, user_gaps, impostor_sims) -> dict:
    """Derive a threshold and margin from measured enrollment scores.

    Args:
        user_sims: each enrollment sample's similarity to the final voiceprint.
        user_gaps: each enrollment sample's (sim_user - sim_jarvis).
        impostor_sims: each JARVIS-TTS sample's similarity to the voiceprint.

    Returns a dict ready to serialize into data/verify_config.json. `separated`
    says whether a clean gap was found; when it is False the two voices score
    too alike for comfort and the caller should say so out loud.
    """
    sims = [float(s) for s in user_sims]
    if not sims:
        raise ValueError("need at least one enrollment similarity to calibrate")

    lo = min(sims)
    hi = max((float(s) for s in impostor_sims), default=None)
    separated = hi is not None and hi < lo - 0.02
    if separated:
        threshold = hi + (lo - hi) * GAP_BIAS
    else:
        # Overlapping (or unmeasured) bands: don't invent a gap, just sit under
        # the weakest real sample.
        threshold = lo - 0.05
    threshold = min(max(threshold, THRESHOLD_MIN), THRESHOLD_MAX)

    gaps = [float(g) for g in user_gaps]
    if gaps:
        margin = min(max(min(gaps) * MARGIN_HEADROOM, MARGIN_MIN), MARGIN_MAX)
    else:
        margin = DEFAULT_MARGIN

    return {
        "threshold": round(threshold, 3),
        "margin": round(margin, 3),
        "separated": separated,
        "user_sim_min": round(lo, 3),
        "user_sim_mean": round(sum(sims) / len(sims), 3),
        "impostor_sim_max": None if hi is None else round(hi, 3),
    }


class VerifyResult(NamedTuple):
    """One verification decision, with the numbers behind it for the log."""

    ok: bool
    sim_user: float
    sim_impostor: float | None
    duration_sec: float
    reason: str


class SpeakerVerifier:
    """Decides whether an utterance is the enrolled speaker.

    Args:
        embed_fn: callable(audio int16 ndarray) -> embedding vector.
        voiceprint: the enrolled unit vector, or None to disable verification
            (not enrolled -> accept everything, so nobody is locked out).
        impostor: optional negative print (JARVIS's own TTS voice). When set,
            an utterance must beat it by `margin` as well as clearing
            `threshold`. When absent the decision degrades to threshold-only.
        threshold: minimum cosine similarity to accept.
        margin: how far sim(user) must exceed sim(impostor).
        min_duration_sec: clips shorter than this skip the absolute threshold
            and are decided by the margin rule alone.
    """

    def __init__(self, *, embed_fn, voiceprint=None, impostor=None,
                 threshold: float = DEFAULT_THRESHOLD,
                 margin: float = DEFAULT_MARGIN,
                 min_duration_sec: float = DEFAULT_MIN_DURATION_SEC,
                 sample_rate: int = 16000):
        self._embed = embed_fn
        self.voiceprint = None if voiceprint is None else np.asarray(voiceprint, dtype=np.float64)
        self.impostor = None if impostor is None else np.asarray(impostor, dtype=np.float64)
        self.threshold = threshold
        self.margin = margin
        self.min_duration_sec = min_duration_sec
        self.sample_rate = sample_rate

    @property
    def enabled(self) -> bool:
        return self.voiceprint is not None

    def decide(self, audio) -> VerifyResult:
        """Score an utterance and explain the verdict.

        Raises on embed failure; `accepts` is the fail-open wrapper that
        callers should use.
        """
        emb = self._embed(audio)
        sim_user = cosine_similarity(emb, self.voiceprint)
        duration = len(audio) / float(self.sample_rate) if len(audio) else 0.0

        sim_imp = None
        if self.impostor is not None:
            candidate = cosine_similarity(emb, self.impostor)
            # A non-finite score means the negative print is unusable; drop back
            # to threshold-only rather than rejecting the user over it.
            if np.isfinite(candidate):
                sim_imp = candidate

        if sim_imp is not None and (sim_user - sim_imp) < self.margin:
            return VerifyResult(False, sim_user, sim_imp, duration, "closer_to_jarvis")

        if duration < self.min_duration_sec and sim_imp is not None:
            # Absolute score untrustworthy at this length; margin rule passed.
            return VerifyResult(True, sim_user, sim_imp, duration, "accept_short_clip")

        if sim_user < self.threshold:
            return VerifyResult(False, sim_user, sim_imp, duration, "below_threshold")
        return VerifyResult(True, sim_user, sim_imp, duration, "accept")

    def accepts(self, audio) -> bool:
        """True if the utterance is the enrolled speaker (or verification off)."""
        if not self.enabled:
            return True
        try:
            d = self.decide(audio)
        except Exception:
            # Fail open: a verification error must never make JARVIS deaf.
            log.warning("speaker verify errored; accepting utterance", exc_info=True)
            return True
        imp = "n/a" if d.sim_impostor is None else f"{d.sim_impostor:.3f}"
        gap = "n/a" if d.sim_impostor is None else f"{d.sim_user - d.sim_impostor:+.3f}"
        log.info(
            "speaker match %.3f (jarvis %s, gap %s | threshold %.2f margin %.2f | "
            "%.1fs) -> %s [%s]",
            d.sim_user, imp, gap, self.threshold, self.margin, d.duration_sec,
            "accept" if d.ok else "reject", d.reason,
        )
        return d.ok


# --- real collaborators (live only; not exercised by unit tests) ------------

def load_encoder(backend: str = DEFAULT_BACKEND):
    """Load the speaker-embedding model for `backend`.

    ecapa       -- SpeechBrain ECAPA-TDNN (192-d). Much better separation
                   between similar voices than the d-vector it replaced, which
                   is what makes the user/echo bands stop overlapping.
    resemblyzer -- the original GE2E d-vector (256-d), kept as an escape hatch
                   so a failed model download cannot lock you out.
    """
    if backend == "resemblyzer":
        from resemblyzer import VoiceEncoder
        return VoiceEncoder()
    from speechbrain.inference.speaker import EncoderClassifier
    from speechbrain.utils.fetching import LocalStrategy
    return EncoderClassifier.from_hparams(
        source="speechbrain/spkrec-ecapa-voxceleb",
        savedir=os.path.join("data", "models", "ecapa"),
        # SpeechBrain symlinks from the HuggingFace cache by default, which
        # needs a privilege Windows only grants to admins or Developer Mode
        # (WinError 1314). Copying costs ~80MB of disk once and always works.
        local_strategy=LocalStrategy.COPY,
    )


def embed_audio(encoder, audio_int16: np.ndarray, backend: str = DEFAULT_BACKEND) -> np.ndarray:
    """Embed 16kHz mono int16 PCM into a speaker vector."""
    if backend == "resemblyzer":
        from resemblyzer import preprocess_wav
        wav = audio_int16.astype(np.float32) / 32768.0
        wav = preprocess_wav(wav, source_sr=16000)
        return encoder.embed_utterance(wav)
    import torch
    wav = torch.from_numpy(audio_int16.astype(np.float32) / 32768.0).unsqueeze(0)
    with torch.no_grad():
        emb = encoder.encode_batch(wav)
    return emb.squeeze().cpu().numpy()


def _load_print(path: str, backend: str, label: str):
    """Load a saved print, refusing one enrolled with a different model."""
    if not os.path.exists(path):
        return None
    vec = np.load(path)
    expected = BACKEND_DIMS.get(backend)
    if expected is not None and vec.shape[-1] != expected:
        log.warning(
            "%s at %s is %d-d but the %s backend produces %d-d embeddings "
            "(enrolled with a different model). Ignoring it - re-enroll with: "
            "python -m clients.enroll_voice",
            label, path, vec.shape[-1], backend, expected,
        )
        return None
    return vec


def load_voiceprint(path: str = VOICEPRINT_PATH, backend: str = DEFAULT_BACKEND):
    return _load_print(path, backend, "Voiceprint")


def load_impostor(path: str = IMPOSTOR_PATH, backend: str = DEFAULT_BACKEND):
    return _load_print(path, backend, "Impostor print")


def load_config(path: str = CONFIG_PATH) -> dict:
    """Calibration written by enrollment. Absent/unreadable -> empty dict.

    Enrollment measures the gap between your voice and JARVIS's and derives the
    threshold from it; writing it here is what stops that number from being a
    suggestion the user has to copy into .env by hand -- which is how the stale
    0.75 default stayed live while the real gap sat elsewhere.
    """
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
        return cfg if isinstance(cfg, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _setting(cfg: dict, env_key: str, cfg_key: str, default: float) -> float:
    """Env var wins, then the enrollment config, then the built-in default."""
    raw = os.getenv(env_key)
    if raw is not None:
        try:
            return float(raw)
        except ValueError:
            log.warning("%s=%r is not a number; ignoring", env_key, raw)
    value = cfg.get(cfg_key)
    if isinstance(value, (int, float)):
        return float(value)
    return default


def from_env():
    """Build a verifier if verification is on AND a voiceprint is enrolled;
    otherwise return None (the server then skips verification entirely)."""
    if os.getenv("SPEAKER_VERIFY", "1") == "0":
        return None
    cfg = load_config()
    backend = os.getenv("SPEAKER_VERIFY_BACKEND") or cfg.get("backend") or DEFAULT_BACKEND
    voiceprint = load_voiceprint(backend=backend)
    if voiceprint is None:
        log.info("Speaker verification idle: no usable voiceprint enrolled "
                 "(run: python -m clients.enroll_voice)")
        return None
    impostor = load_impostor(backend=backend)
    if impostor is None:
        log.info("No negative print for JARVIS's own voice; verification is "
                 "threshold-only and the TTS echo is harder to reject. "
                 "Re-run enrollment to build one.")
    try:
        encoder = load_encoder(backend)
    except Exception:
        # Model package missing, download failed, no network. Verification off
        # is bad; a daemon that won't start is worse.
        log.warning("Could not load the %s speaker model; verification is OFF "
                    "for this run. Install it with: pip install -r requirements.txt",
                    backend, exc_info=True)
        return None
    # Per backend: a threshold is only meaningful on the scale it was measured on.
    threshold = _setting(cfg, "SPEAKER_VERIFY_THRESHOLD", "threshold",
                         BACKEND_DEFAULT_THRESHOLD.get(backend, DEFAULT_THRESHOLD))
    margin = _setting(cfg, "SPEAKER_VERIFY_MARGIN", "margin",
                      BACKEND_DEFAULT_MARGIN.get(backend, DEFAULT_MARGIN))
    min_dur = _setting(cfg, "SPEAKER_VERIFY_MIN_DURATION_SEC", "min_duration_sec",
                       DEFAULT_MIN_DURATION_SEC)
    log.info("Speaker verification: %s backend, threshold %.2f, margin %.2f%s",
             backend, threshold, margin,
             "" if impostor is None else ", negative print loaded")
    return SpeakerVerifier(
        embed_fn=lambda a: embed_audio(encoder, a, backend),
        voiceprint=voiceprint,
        impostor=impostor,
        threshold=threshold,
        margin=margin,
        min_duration_sec=min_dur,
    )
