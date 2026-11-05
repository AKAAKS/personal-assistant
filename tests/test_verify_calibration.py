"""Deriving the threshold and margin from measured scores.

The old enrollment printed `max(0.60, lo - 0.05)` and left it to the user to
copy into .env. Nobody did, so the stale 0.75 default stayed live -- sitting
inside the user's own score band and rejecting 63% of real utterances. These
numbers now come from the measured GAP between the user's voice and JARVIS's,
and get written to disk.
"""

import pytest

from voice.speaker_verify import (
    DEFAULT_MARGIN,
    THRESHOLD_MAX,
    THRESHOLD_MIN,
    calibrate,
)


def test_threshold_lands_in_the_gap_between_the_bands():
    cal = calibrate(
        user_sims=[0.80, 0.85, 0.90],       # the enrolled speaker
        user_gaps=[0.40, 0.50, 0.55],
        impostor_sims=[0.30, 0.45, 0.50],   # JARVIS's own TTS voice
    )
    assert 0.50 < cal["threshold"] < 0.80, cal
    assert cal["separated"] is True


def test_threshold_sits_below_the_midpoint_so_the_user_has_headroom():
    """A false reject is worse than a false accept here: the margin rule is
    the real guard against the echo, so leave room on the user's side."""
    cal = calibrate(user_sims=[0.80], user_gaps=[0.5], impostor_sims=[0.40])
    assert cal["threshold"] < 0.60  # the midpoint would be 0.60


def test_overlapping_bands_fall_back_below_the_weakest_sample():
    """No clean gap: stay under the user's worst score rather than invent one."""
    cal = calibrate(
        user_sims=[0.60, 0.70],
        user_gaps=[0.2, 0.3],
        impostor_sims=[0.55, 0.65],  # overlaps the user band
    )
    assert cal["threshold"] <= 0.60
    assert cal["separated"] is False


def test_no_impostor_samples_still_produces_a_usable_threshold():
    cal = calibrate(user_sims=[0.70, 0.80], user_gaps=[], impostor_sims=[])
    assert cal["threshold"] <= 0.70
    assert cal["margin"] == DEFAULT_MARGIN
    assert cal["separated"] is False


def test_margin_is_derived_from_the_worst_observed_gap_with_headroom():
    cal = calibrate(user_sims=[0.85], user_gaps=[0.40, 0.60], impostor_sims=[0.30])
    assert 0.05 <= cal["margin"] <= 0.40
    assert cal["margin"] < 0.40  # strictly inside the worst gap, not equal to it


def test_a_tiny_gap_still_yields_a_non_zero_margin():
    cal = calibrate(user_sims=[0.70], user_gaps=[0.02], impostor_sims=[0.68])
    assert cal["margin"] >= 0.05


def test_threshold_is_clamped_to_a_sane_range():
    # Absurdly low user scores must not drive the threshold to zero.
    low = calibrate(user_sims=[0.10], user_gaps=[0.01], impostor_sims=[])
    assert low["threshold"] >= THRESHOLD_MIN
    # Nor may a perfect enrollment push it to 1.0, which nothing could clear.
    high = calibrate(user_sims=[1.0], user_gaps=[0.9], impostor_sims=[0.99])
    assert high["threshold"] <= THRESHOLD_MAX


def test_the_floor_suits_the_ecapa_score_scale():
    """ECAPA puts same-speaker at ~0.3-0.8 and different-speaker at or below
    zero; the d-vector this was first written for put same-speaker at 0.7-0.9.
    A 0.40 floor clamped a correctly-derived 0.24 upward and then rejected
    genuine 1-2s utterances scoring 0.29."""
    assert THRESHOLD_MIN <= 0.25

    # The user's real enrollment numbers must derive something that accepts
    # their weakest measured utterance (0.290) rather than clamping above it.
    cal = calibrate(user_sims=[0.594, 0.72], user_gaps=[0.602, 0.70],
                    impostor_sims=[0.002])
    assert cal["threshold"] < 0.290
    assert cal["margin"] < 0.285  # their weakest real gap


def test_rejects_an_empty_user_band():
    with pytest.raises(ValueError):
        calibrate(user_sims=[], user_gaps=[], impostor_sims=[0.3])


# --- a calibration that locks the user out must not report success ---------
#
# `separated` was computed only from user_sims vs impostor_sims, never from the
# GAPS -- while the runtime accept rule is `gap >= margin`. And margin is
# clamped UPWARD by MARGIN_MIN (0.05). So a bad enrollment (noisy room, the
# speaker playing during capture, a negative print built from the wrong voice)
# where the user's own samples sit as close to the JARVIS print as to their own
# yields min(gaps) <= 0, a written margin of 0.05, and separated: true with no
# warning. At runtime the enrolled speaker's gap can never reach 0.05, so EVERY
# utterance rejects as closer_to_jarvis -- total lockout, reported as success.
#
# The invariant: a config is only "separated" if the user's own weakest sample
# would actually be ACCEPTED under the numbers being written.


def test_a_self_locking_calibration_is_not_reported_as_separated():
    cfg = calibrate(
        user_sims=[0.55, 0.60, 0.58],
        user_gaps=[-0.03, 0.01, -0.01],   # at/below the JARVIS print
        impostor_sims=[0.20],
    )
    assert not cfg["separated"], (
        f"reported separated with margin {cfg['margin']} against a weakest "
        f"user gap of -0.03; the enrolled speaker is locked out"
    )


def test_the_written_config_accepts_the_users_own_weakest_sample():
    """The property that matters, stated directly."""
    cases = [
        ([0.55, 0.60, 0.58], [-0.03, 0.01, -0.01], [0.20]),
        ([0.30, 0.35, 0.42], [0.31, 0.29, 0.46], [0.24, 0.19]),
        ([0.70, 0.75], [0.60, 0.65], [0.10]),
    ]
    for user_sims, user_gaps, impostor_sims in cases:
        cfg = calibrate(user_sims=user_sims, user_gaps=user_gaps,
                        impostor_sims=impostor_sims)
        if not cfg["separated"]:
            continue   # already flagged; the caller warns
        assert min(user_gaps) >= cfg["margin"], (
            f"separated config rejects its own enrollment: weakest gap "
            f"{min(user_gaps)} < margin {cfg['margin']}"
        )
        assert min(user_sims) >= cfg["threshold"], (
            f"separated config rejects its own enrollment: weakest sim "
            f"{min(user_sims)} < threshold {cfg['threshold']}"
        )


def test_a_healthy_calibration_is_still_separated():
    """The good path must not get caught by the new check."""
    cfg = calibrate(
        user_sims=[0.62, 0.70, 0.68],
        user_gaps=[0.55, 0.62, 0.60],
        impostor_sims=[0.12, 0.08],
    )
    assert cfg["separated"]
    assert min([0.55, 0.62, 0.60]) >= cfg["margin"]
