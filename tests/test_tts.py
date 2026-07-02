import pytest


@pytest.mark.slow  # loads SAPI5; still offline and fast (~1s)
def test_synthesize_returns_wav_bytes():
    from voice.tts import TextToSpeech
    tts = TextToSpeech(rate=200)
    wav = tts.synthesize("hello")
    assert wav is not None and wav[:4] == b"RIFF"
    assert tts.synthesize("") is None


def test_synthesize_returns_none_on_engine_failure():
    """SAPI5/COM errors must not propagate; synthesize degrades to None."""
    from voice.tts import TextToSpeech

    class BrokenEngine:
        def save_to_file(self, text, path):
            raise RuntimeError("COM error: SAPI5 unavailable")

        def runAndWait(self):
            raise AssertionError("should not be reached")

    tts = TextToSpeech.__new__(TextToSpeech)  # skip __init__ (no real SAPI5)
    tts.engine = BrokenEngine()
    assert tts.synthesize("hello") is None
