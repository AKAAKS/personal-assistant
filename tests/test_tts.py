import pytest


@pytest.mark.slow  # loads SAPI5; still offline and fast (~1s)
def test_synthesize_returns_wav_bytes():
    from voice.tts import TextToSpeech
    tts = TextToSpeech(rate=200)
    wav = tts.synthesize("hello")
    assert wav is not None and wav[:4] == b"RIFF"
    assert tts.synthesize("") is None
