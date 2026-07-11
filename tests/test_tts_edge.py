"""Tests for the optional edge-tts neural voice backend and the make_tts factory.

Offline by design: every test except the one marked @pytest.mark.slow monkeypatches
the network-touching pieces (edge_tts.Communicate / miniaudio.decode) so `pytest tests/`
never dials out. The slow test does a real round trip and skips itself if offline.
"""

import os

import pytest


# ---------------------------------------------------------------------------
# make_tts factory
# ---------------------------------------------------------------------------

def test_make_tts_pyttsx3_backend_returns_texttospeech():
    from voice.tts import TextToSpeech, make_tts
    tts = make_tts("pyttsx3")
    assert isinstance(tts, TextToSpeech)


def test_make_tts_defaults_to_pyttsx3_when_env_unset(monkeypatch):
    monkeypatch.delenv("TTS_BACKEND", raising=False)
    from voice.tts import TextToSpeech, make_tts
    tts = make_tts()
    assert isinstance(tts, TextToSpeech)


def test_make_tts_unknown_backend_falls_back_to_texttospeech():
    from voice.tts import TextToSpeech, make_tts
    tts = make_tts("something-bogus")
    assert isinstance(tts, TextToSpeech)


def test_make_tts_edge_backend_has_synthesize_method():
    from voice.tts import make_tts
    tts = make_tts("edge")
    assert hasattr(tts, "synthesize")
    assert callable(tts.synthesize)


def test_make_tts_edge_does_not_pass_sapi_voice_id_to_edge(monkeypatch):
    # A SAPI voice GUID (TTS_VOICE_ID / voice_id) must NOT be handed to EdgeTTS
    # as its voice name — that would silently break every edge synthesis and
    # force a permanent pyttsx3 fallback. Edge reads EDGE_TTS_VOICE itself.
    import voice.tts as tts_mod
    captured = {}

    class FakeEdge:
        def __init__(self, *a, **k):
            captured["args"] = a
            captured["kwargs"] = k

    monkeypatch.setattr("voice.tts_edge.EdgeTTS", FakeEdge)
    tts_mod.make_tts("edge", voice_id="{SAPI-GUID-Zira}")

    assert captured["args"] == ()
    assert "voice" not in captured["kwargs"] or captured["kwargs"]["voice"] is None


# ---------------------------------------------------------------------------
# EdgeTTS.synthesize — offline behavior
# ---------------------------------------------------------------------------

def test_edge_synthesize_empty_text_returns_none_no_network():
    from voice.tts_edge import EdgeTTS
    edge = EdgeTTS()
    assert edge.synthesize("") is None
    assert edge.synthesize("   ") is None


def test_edge_synthesize_returns_none_when_gen_raises(monkeypatch):
    """Simulate network failure / edge-tts error inside the worker thread."""
    from voice.tts_edge import EdgeTTS
    edge = EdgeTTS()

    def _boom(self, text, voice):
        raise RuntimeError("simulated network failure")

    monkeypatch.setattr(EdgeTTS, "_generate_mp3", _boom)
    assert edge.synthesize("hello there", timeout=5) is None


def test_edge_synthesize_returns_none_on_timeout(monkeypatch):
    import time as time_mod
    from voice.tts_edge import EdgeTTS
    edge = EdgeTTS()

    def _hang(self, text, voice):
        time_mod.sleep(5)
        return b"fake mp3 bytes"

    monkeypatch.setattr(EdgeTTS, "_generate_mp3", _hang)
    start = time_mod.monotonic()
    assert edge.synthesize("hello there", timeout=0.2) is None
    assert time_mod.monotonic() - start < 2


def test_edge_synthesize_wraps_decoded_pcm_as_riff_wav(monkeypatch):
    """Exercise the wave-wrapping logic without a real MP3 or network call."""
    from voice.tts_edge import EdgeTTS
    edge = EdgeTTS()

    monkeypatch.setattr(
        EdgeTTS, "_generate_mp3", lambda self, text, voice: b"not-really-mp3-bytes"
    )

    class FakeDecoded:
        nchannels = 1
        sample_rate = 24000
        samples = bytearray(b"\x00\x01" * 100)  # 100 frames of 16-bit mono silence-ish

    import voice.tts_edge as tts_edge_mod

    def _fake_decode(mp3_bytes, output_format=None, nchannels=None, sample_rate=None):
        assert mp3_bytes == b"not-really-mp3-bytes"
        return FakeDecoded()

    monkeypatch.setattr(tts_edge_mod, "_miniaudio_decode", _fake_decode)

    wav = edge.synthesize("hello there", timeout=5)
    assert wav is not None
    assert wav[:4] == b"RIFF"
    assert wav[8:12] == b"WAVE"


def test_edge_synthesize_returns_none_when_decode_raises(monkeypatch):
    from voice.tts_edge import EdgeTTS
    edge = EdgeTTS()

    monkeypatch.setattr(EdgeTTS, "_generate_mp3", lambda self, text, voice: b"garbage")

    import voice.tts_edge as tts_edge_mod

    def _fake_decode(*args, **kwargs):
        raise ValueError("bad mp3 data")

    monkeypatch.setattr(tts_edge_mod, "_miniaudio_decode", _fake_decode)
    assert edge.synthesize("hello there", timeout=5) is None


# ---------------------------------------------------------------------------
# make_tts("edge") pyttsx3 fallback
# ---------------------------------------------------------------------------

def test_make_tts_edge_falls_back_to_pyttsx3_when_edge_returns_none(monkeypatch):
    from voice.tts_edge import EdgeTTS
    import voice.tts as tts_mod

    monkeypatch.setattr(EdgeTTS, "synthesize", lambda self, text, timeout=20: None)

    class FakeFallback:
        def synthesize(self, text, timeout=20):
            return b"RIFFxxxx"

    monkeypatch.setattr(tts_mod, "TextToSpeech", lambda *a, **kw: FakeFallback())

    tts = tts_mod.make_tts("edge")
    result = tts.synthesize("hello there")
    assert result == b"RIFFxxxx"


def test_make_tts_edge_does_not_call_fallback_when_edge_succeeds(monkeypatch):
    from voice.tts_edge import EdgeTTS
    import voice.tts as tts_mod

    monkeypatch.setattr(EdgeTTS, "synthesize", lambda self, text, timeout=20: b"RIFFedge")

    def _fail_fallback(*a, **kw):
        raise AssertionError("fallback should not be constructed when edge succeeds")

    monkeypatch.setattr(tts_mod, "TextToSpeech", _fail_fallback)

    tts = tts_mod.make_tts("edge")
    result = tts.synthesize("hello there")
    assert result == b"RIFFedge"


# ---------------------------------------------------------------------------
# Real network round trip (slow, skip if offline)
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_edge_synthesize_real_network_round_trip():
    from voice.tts_edge import EdgeTTS
    edge = EdgeTTS()
    try:
        wav = edge.synthesize("Testing edge text to speech.", timeout=20)
    except Exception as exc:  # pragma: no cover - defensive; synthesize should not raise
        pytest.skip(f"network error calling edge-tts: {exc}")
    if wav is None:
        pytest.skip("edge-tts synthesis returned None (likely offline or endpoint unreachable)")
    assert wav[:4] == b"RIFF"
    assert wav[8:12] == b"WAVE"
