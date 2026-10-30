"""Speaker verification — respond only to the enrolled voice.

The embedding model (Resemblyzer) is injected, so these tests exercise the
accept/reject decision and enrollment averaging with plain vectors — no model,
no audio.
"""

import numpy as np
import pytest

from voice.speaker_verify import SpeakerVerifier, cosine_similarity, make_voiceprint


def test_cosine_identical_is_one():
    assert cosine_similarity([1, 2, 3], [1, 2, 3]) == pytest.approx(1.0)


def test_cosine_orthogonal_is_zero():
    assert cosine_similarity([1, 0], [0, 1]) == pytest.approx(0.0)


def test_cosine_zero_vector_is_zero():
    assert cosine_similarity([0, 0], [1, 1]) == 0.0


def test_make_voiceprint_averages_and_normalizes():
    vp = make_voiceprint([[2, 0, 0], [0, 0, 0]])  # mean [1,0,0] -> unit [1,0,0]
    assert np.allclose(vp, [1, 0, 0])
    assert np.linalg.norm(vp) == pytest.approx(1.0)


def test_make_voiceprint_rejects_empty():
    with pytest.raises(ValueError):
        make_voiceprint([])


def _verifier(embed, voiceprint=(1.0, 0.0, 0.0), threshold=0.75):
    return SpeakerVerifier(embed_fn=lambda a: embed, voiceprint=voiceprint, threshold=threshold)


def test_disabled_when_no_voiceprint_accepts_everything():
    v = SpeakerVerifier(embed_fn=lambda a: [0, 1, 0], voiceprint=None)
    assert not v.enabled
    assert v.accepts(np.zeros(1600)) is True


def test_matching_voice_is_accepted():
    assert _verifier([1.0, 0.0, 0.0]).accepts(np.zeros(1600)) is True


def test_different_voice_is_rejected():
    assert _verifier([0.0, 1.0, 0.0]).accepts(np.zeros(1600)) is False


def test_threshold_is_respected():
    # similarity ~0.71 (45°) — below 0.75 default, above a lax 0.6
    emb = [1.0, 1.0, 0.0]
    assert _verifier(emb, threshold=0.75).accepts(np.zeros(1600)) is False
    assert _verifier(emb, threshold=0.60).accepts(np.zeros(1600)) is True


def test_embed_failure_fails_open():
    def boom(_a):
        raise RuntimeError("model died")
    v = SpeakerVerifier(embed_fn=boom, voiceprint=(1.0, 0.0, 0.0))
    assert v.accepts(np.zeros(1600)) is True  # never make JARVIS deaf on error
