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
