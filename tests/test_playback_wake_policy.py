"""Deciding whether a wake-word hit DURING playback is really the user.

While a reply plays, the mic also hears JARVIS coming back off the room
speaker, so the idle wake-word threshold is the wrong bar. The policy adds two
discriminators that cost nothing and need no echo canceller:

  * a stricter score threshold than idle listening, and
  * the chunk must be clearly louder than the running echo floor -- you
    speaking near the mic beats the speaker's bleed -- repeated over
    consecutive chunks so a single fluke can't fire.

Pure arithmetic: no audio, no openwakeword.
"""

from clients.voice_client import PlaybackWakePolicy


def _policy(**kw):
    kw.setdefault("threshold", 0.7)
    kw.setdefault("consecutive", 2)
    kw.setdefault("level_ratio", 2.0)
    kw.setdefault("min_level", 200.0)
    return PlaybackWakePolicy(**kw)


def test_loud_confident_repeated_hit_fires():
    p = _policy()
    for _ in range(10):  # establish an echo floor of 500
        p.observe(score=0.0, level=500.0)
    assert p.observe(score=0.9, level=4000.0) is False  # 1 of 2
    assert p.observe(score=0.9, level=4000.0) is True   # 2 of 2


def test_a_single_hit_does_not_fire():
    p = _policy()
    for _ in range(10):
        p.observe(score=0.0, level=500.0)
    assert p.observe(score=0.9, level=4000.0) is False


def test_the_streak_resets_on_a_miss():
    p = _policy()
    for _ in range(10):
        p.observe(score=0.0, level=500.0)
    assert p.observe(score=0.9, level=4000.0) is False
    assert p.observe(score=0.1, level=4000.0) is False  # breaks the streak
    assert p.observe(score=0.9, level=4000.0) is False  # back to 1 of 2


def test_echo_level_hit_is_rejected_however_confident():
    """The self-fire case: JARVIS's own voice scores high but is no louder
    than the echo floor it established."""
    p = _policy()
    for _ in range(10):
        p.observe(score=0.0, level=500.0)
    assert p.observe(score=0.99, level=600.0) is False
    assert p.observe(score=0.99, level=600.0) is False
    assert p.observe(score=0.99, level=600.0) is False


def test_idle_threshold_would_have_fired_but_playback_threshold_does_not():
    p = _policy(threshold=0.7, consecutive=1)
    for _ in range(10):
        p.observe(score=0.0, level=500.0)
    assert p.observe(score=0.55, level=4000.0) is False  # passes 0.5, not 0.7
    assert p.observe(score=0.75, level=4000.0) is True


def test_the_echo_floor_ignores_candidate_chunks():
    """Your own speech must not raise the floor, or a long sentence would
    gradually gate itself out."""
    p = _policy(consecutive=1)
    for _ in range(10):
        p.observe(score=0.0, level=500.0)
    quiet_floor = p.echo_floor
    for _ in range(10):
        p.observe(score=0.9, level=9000.0)  # loud candidates
    assert p.echo_floor == quiet_floor


def test_silence_cannot_fire_via_the_zero_floor():
    """Before any echo arrives the floor is 0; an absolute minimum level stops
    a near-silent chunk from clearing `floor * ratio` trivially."""
    p = _policy(consecutive=1)
    assert p.echo_floor == 0.0
    assert p.observe(score=0.99, level=50.0) is False   # under min_level
    assert p.observe(score=0.99, level=5000.0) is True


def test_reset_clears_the_streak_and_the_floor():
    p = _policy()
    for _ in range(5):
        p.observe(score=0.0, level=900.0)
    p.observe(score=0.9, level=9000.0)
    p.reset()
    assert p.echo_floor == 0.0
    assert p.observe(score=0.9, level=9000.0) is False  # streak restarted
