import time

import pytest


@pytest.mark.slow  # loads SAPI5; still offline and fast (~2s)
def test_synthesize_twice_returns_wav_bytes():
    """Two consecutive syntheses must both succeed: a shared SAPI5 engine
    used to deadlock on the second runAndWait() (reply never sent)."""
    from voice.tts import TextToSpeech
    tts = TextToSpeech(rate=200)
    first = tts.synthesize("hello")
    second = tts.synthesize("still alive")
    assert first is not None and first[:4] == b"RIFF"
    assert second is not None and second[:4] == b"RIFF"
    assert tts.synthesize("") is None


def _bare_tts(engine_factory):
    from voice.tts import TextToSpeech
    tts = TextToSpeech.__new__(TextToSpeech)  # skip __init__ (no real SAPI5)
    tts.rate = 180
    tts.voice_id = None
    tts._engine_factory = engine_factory
    return tts


def test_synthesize_returns_none_on_engine_failure():
    """SAPI5/COM errors must not propagate; synthesize degrades to None."""

    class BrokenEngine:
        def save_to_file(self, text, path):
            raise RuntimeError("COM error: SAPI5 unavailable")

        def runAndWait(self):
            raise AssertionError("should not be reached")

    tts = _bare_tts(lambda: BrokenEngine())
    assert tts.synthesize("hello") is None


def test_synthesize_returns_none_on_hang():
    """A hung engine must trip the timeout, not freeze the reply forever."""

    class HangingEngine:
        def save_to_file(self, text, path):
            pass

        def runAndWait(self):
            time.sleep(5)

    tts = _bare_tts(lambda: HangingEngine())
    start = time.monotonic()
    assert tts.synthesize("hello", timeout=0.2) is None
    assert time.monotonic() - start < 2
