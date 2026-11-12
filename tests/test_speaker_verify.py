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


# --- two-class decision: the user's print AND JARVIS's own voice ------------
#
# A single absolute threshold could not separate "me over a BT mic" from
# "my TTS voice over a BT mic" -- the live logs showed the two bands
# overlapping (me: 0.666-0.868, echo: 0.375-0.630) with the threshold sitting
# inside mine. Scoring against a negative print of JARVIS's own voice makes the
# decision relative, so the echo is rejected for being *closer to JARVIS* no
# matter where the absolute threshold drifts.

USER = (1.0, 0.0, 0.0)
JARVIS = (0.0, 1.0, 0.0)


def _two_class(embed, *, threshold=0.60, margin=0.10, min_duration_sec=1.2):
    return SpeakerVerifier(
        embed_fn=lambda a: embed,
        voiceprint=USER,
        impostor=JARVIS,
        threshold=threshold,
        margin=margin,
        min_duration_sec=min_duration_sec,
    )


def _clip(seconds=3.0):
    return np.zeros(int(16000 * seconds), dtype=np.int16)


def test_clip_closer_to_jarvis_is_rejected_even_above_threshold():
    """The echo case: a high absolute score that still leans toward JARVIS."""
    # 15 deg off JARVIS, 75 deg off the user -> sim_user .26, sim_jarvis .97
    emb = [0.26, 0.97, 0.0]
    v = _two_class(emb, threshold=0.10)  # threshold deliberately permissive
    assert v.accepts(_clip()) is False
    assert v.decide(_clip()).reason == "closer_to_jarvis"


def test_clip_closer_to_the_user_is_accepted():
    emb = [0.97, 0.26, 0.0]
    v = _two_class(emb)
    assert v.accepts(_clip()) is True
    assert v.decide(_clip()).reason == "accept"


def test_margin_boundary_is_inclusive():
    # sim_user - sim_jarvis is exactly the margin -> accept
    emb = [0.8, 0.7, 0.0]
    exact = cosine_similarity(emb, USER) - cosine_similarity(emb, JARVIS)
    assert _two_class(emb, threshold=0.10, margin=exact).accepts(_clip()) is True
    assert _two_class(emb, threshold=0.10, margin=exact + 0.01).accepts(_clip()) is False


def test_absolute_threshold_still_applies_on_a_long_clip():
    """Passing the margin is necessary but not sufficient."""
    emb = [0.30, 0.0, 0.95]  # nothing like JARVIS, but nothing like the user
    v = _two_class(emb, threshold=0.60)
    assert v.accepts(_clip()) is False
    assert v.decide(_clip()).reason == "below_threshold"


def test_short_clip_is_decided_by_the_margin_alone():
    """Embeddings from sub-second audio are noisy, so the absolute score is not
    trustworthy -- but 'which voice is this closer to' still is."""
    emb = [0.55, 0.10, 0.0]  # sim_user .98 but would fail a 0.60 threshold? no:
    v = _two_class(emb, threshold=0.99, min_duration_sec=1.2)
    short = _clip(0.4)
    assert v.accepts(short) is True  # threshold 0.99 ignored on a short clip
    assert v.decide(short).reason == "accept_short_clip"
    assert v.accepts(_clip(3.0)) is False  # same audio content, long -> threshold bites


def test_short_clip_still_rejected_when_it_leans_to_jarvis():
    v = _two_class([0.26, 0.97, 0.0], threshold=0.10)
    assert v.accepts(_clip(0.4)) is False


def test_without_an_impostor_print_it_is_threshold_only():
    """Degrade to the old single-threshold behaviour, never to deafness."""
    v = SpeakerVerifier(
        embed_fn=lambda a: [0.97, 0.26, 0.0], voiceprint=USER, threshold=0.60
    )
    assert v.impostor is None
    assert v.accepts(_clip()) is True
    assert v.decide(_clip()).sim_impostor is None


def test_decide_reports_the_numbers_for_logging():
    d = _two_class([0.97, 0.26, 0.0]).decide(_clip(2.5))
    assert d.sim_user == pytest.approx(0.966, abs=0.01)
    assert d.sim_impostor == pytest.approx(0.259, abs=0.01)
    assert d.duration_sec == pytest.approx(2.5, abs=0.01)
    assert d.ok is True


def test_impostor_embed_failure_falls_back_to_threshold_not_deafness():
    """A broken impostor comparison must not reject the user."""
    bad = np.array([np.nan, np.nan, np.nan])
    v = SpeakerVerifier(
        embed_fn=lambda a: [1.0, 0.0, 0.0], voiceprint=USER, impostor=bad,
        threshold=0.60,
    )
    assert v.accepts(_clip()) is True


# --- stale-print and calibration-config handling ----------------------------

from voice.speaker_verify import _load_print, _setting, load_config  # noqa: E402


def test_load_print_refuses_a_print_from_the_other_backend(tmp_path):
    """A 256-d Resemblyzer print must not be scored against 192-d ECAPA
    embeddings -- that would produce garbage and reject everything."""
    p = tmp_path / "voiceprint.npy"
    np.save(p, np.zeros(256))
    assert _load_print(str(p), "ecapa", "Voiceprint") is None
    assert _load_print(str(p), "resemblyzer", "Voiceprint") is not None


def test_load_print_accepts_matching_dimension(tmp_path):
    p = tmp_path / "voiceprint.npy"
    np.save(p, np.ones(192))
    vec = _load_print(str(p), "ecapa", "Voiceprint")
    assert vec is not None and vec.shape == (192,)


def test_load_print_missing_file_is_none():
    assert _load_print("does/not/exist.npy", "ecapa", "Voiceprint") is None


def test_load_config_missing_or_bad_is_empty(tmp_path):
    assert load_config(str(tmp_path / "nope.json")) == {}
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert load_config(str(bad)) == {}


def test_setting_precedence_env_then_config_then_default(monkeypatch):
    cfg = {"threshold": 0.62}
    monkeypatch.delenv("SPEAKER_VERIFY_THRESHOLD", raising=False)
    assert _setting(cfg, "SPEAKER_VERIFY_THRESHOLD", "threshold", 0.75) == 0.62
    assert _setting({}, "SPEAKER_VERIFY_THRESHOLD", "threshold", 0.75) == 0.75
    monkeypatch.setenv("SPEAKER_VERIFY_THRESHOLD", "0.70")
    assert _setting(cfg, "SPEAKER_VERIFY_THRESHOLD", "threshold", 0.75) == 0.70


def test_setting_ignores_a_non_numeric_env_var(monkeypatch):
    monkeypatch.setenv("SPEAKER_VERIFY_THRESHOLD", "high")
    assert _setting({"threshold": 0.62}, "SPEAKER_VERIFY_THRESHOLD", "threshold", 0.75) == 0.62


# --- a short clip must still clear SOME absolute floor ----------------------
#
# decide() used to return accept_short_clip for ANY clip under min_duration_sec
# once the margin rule passed, skipping self.threshold entirely. Since
# record_until_silence closes a capture after 800ms of trailing silence, almost
# every real command ("lights off", "play music") is 1.0-1.8s -- so the absolute
# check was bypassed for the COMMON case, not an edge case.
#
# The hole: another person, or the TV, is a human voice and therefore far from
# JARVIS's TTS print. sim_user ~0.10 (not the enrolled speaker) with sim_imp
# ~-0.20 gives a gap of 0.30, which clears a 0.24 margin -- accepted as the
# enrolled speaker while scoring far BELOW the threshold. That directly
# contradicts this module's promise to reject "random triggers from other
# people or the TV".
#
# The live data hid this: every genuine row measured was short (1.2-1.6s) and
# every impostor row was long (>= 2.2s), so no test ever drove a SHORT impostor
# through decide().


def _floor_verifier(**kw):
    emb = kw.pop("emb")
    opts = dict(
        embed_fn=lambda a: emb,
        voiceprint=(1.0, 0.0, 0.0),
        impostor=(0.0, 1.0, 0.0),
        threshold=0.25,
        margin=0.24,
        min_duration_sec=2.0,
    )
    opts.update(kw)
    return SpeakerVerifier(**opts)


def _floor_emb(sim_user, sim_imp):
    e = np.array([sim_user, sim_imp, 0.0], dtype=np.float64)
    extra = 1.0 - (sim_user**2 + sim_imp**2)
    e[2] = np.sqrt(extra) if extra > 0 else 0.0
    return e


def _decide_short(sim_user, sim_imp, duration=1.4):
    v = _floor_verifier(emb=_floor_emb(sim_user, sim_imp))
    return v.decide(np.zeros(int(16000 * duration), dtype=np.int16))


def test_a_short_clip_from_another_person_is_rejected():
    """The regression this whole block exists for."""
    d = _decide_short(0.10, -0.20)
    assert not d.ok, (
        f"accepted another speaker on a 1.4s clip [{d.reason}]: sim_user 0.10 "
        "is far below the 0.25 threshold, it only passed on the gap"
    )


def test_a_short_clip_from_the_tv_is_rejected():
    """Broadcast speech: not the user, nothing like the TTS voice."""
    d = _decide_short(0.05, -0.30)
    assert not d.ok


def test_a_genuine_short_clip_is_still_accepted():
    """The leniency the short-clip path exists for must survive: the live log's
    weakest genuine short rows were 0.290 at 1.6s and 0.350 at 1.2s."""
    for sim_user, sim_imp, dur in [(0.290, -0.017, 1.6), (0.350, 0.065, 1.2),
                                   (0.422, -0.045, 1.4)]:
        d = _decide_short(sim_user, sim_imp, dur)
        assert d.ok, f"rejected real speech [{d.reason}]: {sim_user} at {dur}s"


def test_a_short_clip_is_judged_more_leniently_than_a_long_one():
    """Not simply the full threshold: a 1-2s embedding is genuinely noisier, and
    tightening all the way would reinstate the false rejections that started
    this."""
    weak = 0.20   # below threshold 0.25, above the relaxed short-clip floor
    assert _decide_short(weak, -0.10, duration=1.2).ok
    assert not _decide_short(weak, -0.10, duration=3.0).ok
