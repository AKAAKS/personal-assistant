"""Regression test built from REAL measured scores, not invented ones.

Every row below was logged by the live pipeline on 2026-11-04 (logs/jarvisd.log)
on the user's laptop mic + Bluetooth room speaker, with ECAPA embeddings. The
first tuning pass got two of them wrong -- genuine 1.2s and 1.6s utterances
rejected -- because the threshold floor and margin headroom had been written
for Resemblyzer's score scale, where same-speaker sits at 0.7-0.9 instead of
ECAPA's 0.3-0.8.

Keeping the real numbers here means a future change to the thresholds, the
constants or the short-clip rule has to stay consistent with what the hardware
actually produced, instead of with someone's idea of what a cosine should be.

The verifier is driven through a stub embed_fn that replays each row's
similarities, so no model and no audio are involved.
"""

import numpy as np
import pytest

from voice.speaker_verify import SpeakerVerifier

# The live settings (.env), chosen at the midpoint of each measured boundary.
THRESHOLD = 0.25
MARGIN = 0.24
MIN_DURATION = 2.0

# (sim_user, sim_jarvis, duration_sec) -- all genuine speech from the enrolled
# speaker, said normally. The last two are the ones the first pass rejected.
GENUINE = [
    (0.628, -0.062, 2.1),
    (0.609, -0.104, 2.5),
    (0.668, -0.063, 2.5),
    (0.686, -0.028, 2.8),
    (0.762, -0.026, 5.7),
    (0.422, -0.045, 1.4),
    (0.775, -0.068, 2.5),
    (0.685, -0.077, 2.4),
    (0.720, -0.081, 2.6),
    (0.666, -0.050, 2.6),
    (0.562, -0.019, 4.1),
    (0.718, 0.036, 3.9),
    (0.664, 0.035, 3.5),
    (0.290, -0.017, 1.6),   # was rejected: below_threshold
    (0.350, 0.065, 1.2),    # was rejected: closer_to_jarvis
]

# Captured while the user talked over a playing reply, so the clip holds BOTH
# voices. Correctly rejected: the turn is unusable either way, and these are
# the closest thing to the echo that the live run produced.
MIXED_WITH_JARVIS = [
    (-0.048, 0.096, 11.1),
    (0.021, 0.123, 4.6),
    (0.242, 0.046, 2.2),
    (0.189, 0.057, 2.3),
]


def _decide(sim_user, sim_jarvis, duration):
    """Run the real decision logic against a row's numbers."""
    # Scale so the unit-normalised embedding reproduces the logged cosines.
    emb = np.array([sim_user, sim_jarvis, 0.0], dtype=np.float64)
    extra = 1.0 - (sim_user**2 + sim_jarvis**2)
    emb[2] = np.sqrt(extra) if extra > 0 else 0.0
    v = SpeakerVerifier(
        embed_fn=lambda a: emb,
        voiceprint=(1.0, 0.0, 0.0),
        impostor=(0.0, 1.0, 0.0),
        threshold=THRESHOLD,
        margin=MARGIN,
        min_duration_sec=MIN_DURATION,
    )
    audio = np.zeros(int(16000 * duration), dtype=np.int16)
    return v.decide(audio)


@pytest.mark.parametrize("sim_user,sim_jarvis,duration", GENUINE)
def test_every_genuine_utterance_is_accepted(sim_user, sim_jarvis, duration):
    d = _decide(sim_user, sim_jarvis, duration)
    assert d.ok, (
        f"rejected real speech [{d.reason}]: sim {sim_user} vs jarvis "
        f"{sim_jarvis} (gap {sim_user - sim_jarvis:+.3f}) at {duration}s"
    )


@pytest.mark.parametrize("sim_user,sim_jarvis,duration", MIXED_WITH_JARVIS)
def test_audio_mixed_with_jarvis_is_rejected(sim_user, sim_jarvis, duration):
    d = _decide(sim_user, sim_jarvis, duration)
    assert not d.ok, (
        f"accepted JARVIS-mixed audio [{d.reason}]: sim {sim_user} vs jarvis "
        f"{sim_jarvis} (gap {sim_user - sim_jarvis:+.3f}) at {duration}s"
    )


def test_the_short_clip_rule_is_what_saves_the_sub_2s_utterances():
    """Both previously-rejected rows are under MIN_DURATION and score below the
    absolute threshold; they pass only because the gap rule decides instead."""
    for sim_user, sim_jarvis, duration in [(0.290, -0.017, 1.6), (0.350, 0.065, 1.2)]:
        d = _decide(sim_user, sim_jarvis, duration)
        assert duration < MIN_DURATION
        assert d.reason == "accept_short_clip"


def test_the_measured_bands_do_not_overlap():
    """The live settings sit between the two bands with headroom on each side.
    If a future change narrows this, the thresholds need re-measuring, not
    nudging."""
    genuine_min_gap = min(u - j for u, j, _ in GENUINE)
    mixed_max_gap = max(u - j for u, j, _ in MIXED_WITH_JARVIS)
    assert mixed_max_gap < MARGIN < genuine_min_gap
    assert genuine_min_gap - MARGIN > 0.03
    assert MARGIN - mixed_max_gap > 0.03
