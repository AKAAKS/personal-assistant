"""Every caller must build TTS the same way the daemon does.

The negative voiceprint is the core defence against the feedback loop: an echo
is rejected because it is closer to JARVIS's voice than to the user's. That only
holds if the voice enrollment synthesizes is the voice the daemon SPEAKS with.

enroll_voice and voice_calibrate both called `make_tts(rate=...)` -- omitting
voice_id AND the positional `backend`. So with TTS_BACKEND=edge the daemon spoke
with an Edge neural voice while enrollment built the negative print from offline
pyttsx3: a completely different voice. The margin rule was then measured against
a voice JARVIS never uses, and the `margin`/`separated` numbers written to
verify_config.json described nothing real.

Centralized so the three call sites cannot drift again.
"""

import voice.tts as tts_mod


def _capture(monkeypatch, **env):
    seen = {}
    monkeypatch.setattr(tts_mod, "make_tts",
                        lambda backend=None, rate=180, voice_id=None: seen.update(
                            backend=backend, rate=rate, voice_id=voice_id))
    for k in ("TTS_BACKEND", "TTS_RATE", "TTS_VOICE_ID"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    tts_mod.make_tts_from_env()
    return seen


def test_the_configured_backend_is_honoured(monkeypatch):
    seen = _capture(monkeypatch, TTS_BACKEND="edge")
    assert seen["backend"] == "edge", (
        "the backend was dropped; the negative print would be built from a "
        "different voice than the daemon speaks with"
    )


def test_the_configured_voice_id_is_honoured(monkeypatch):
    seen = _capture(monkeypatch, TTS_VOICE_ID="Microsoft David Desktop")
    assert seen["voice_id"] == "Microsoft David Desktop"


def test_rate_is_honoured_and_defaults_match_the_daemon(monkeypatch):
    assert _capture(monkeypatch)["rate"] == 180
    assert _capture(monkeypatch, TTS_RATE="210")["rate"] == 210


def test_an_empty_voice_id_becomes_none(monkeypatch):
    """`TTS_VOICE_ID=` in a .env must mean "default", not a voice named ""."""
    assert _capture(monkeypatch, TTS_VOICE_ID="")["voice_id"] is None


def test_enrollment_and_calibration_use_the_shared_builder():
    """Static guarantee: no call site may hand-roll make_tts(...) again."""
    import inspect

    import clients.enroll_voice as enroll
    for mod in (enroll,):
        src = inspect.getsource(mod)
        assert "make_tts_from_env()" in src, f"{mod.__name__} hand-rolls TTS"
