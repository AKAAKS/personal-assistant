"""What SpeechToText.transcribe passes to Whisper.

The log showed "terminal" -> "domino", "Claude" -> "flawed code" and lost first
words. Two of the three causes are in this call: no initial_prompt to anchor the
domain vocabulary, and vad_filter=True trimming audio that voice/audio_io.py had
ALREADY VAD-trimmed -- a second trim that ate word onsets.

The model is faked via __new__, so no Whisper weights are loaded.
"""

import numpy as np
import pytest

from voice.stt import DEFAULT_INITIAL_PROMPT, SpeechToText


class Seg:
    def __init__(self, text, no_speech_prob=0.0, avg_logprob=0.0):
        self.text = text
        self.no_speech_prob = no_speech_prob
        self.avg_logprob = avg_logprob


class FakeModel:
    """Records the kwargs transcribe() was called with."""

    def __init__(self, segments=None):
        self.segments = segments if segments is not None else [Seg("lights on")]
        self.calls = []

    def transcribe(self, audio, **kwargs):
        self.calls.append((audio, kwargs))
        return iter(self.segments), None


def _stt(model=None, **attrs):
    s = SpeechToText.__new__(SpeechToText)  # skip __init__: no model download
    s.model = model or FakeModel()
    s.initial_prompt = attrs.get("initial_prompt", DEFAULT_INITIAL_PROMPT)
    s.vad_filter = attrs.get("vad_filter", False)
    s.beam_size = attrs.get("beam_size", 5)
    return s


def test_initial_prompt_is_passed_to_whisper():
    s = _stt()
    s.transcribe(np.zeros(16000, dtype=np.int16))
    _, kwargs = s.model.calls[0]
    assert kwargs["initial_prompt"] == DEFAULT_INITIAL_PROMPT


def test_default_prompt_names_the_mangled_vocabulary():
    """These are the exact words the live log got wrong."""
    low = DEFAULT_INITIAL_PROMPT.lower()
    for word in ("jarvis", "claude", "terminal", "spotify"):
        assert word in low, f"{word!r} missing from the STT prompt"


def test_vad_filter_defaults_off_so_onsets_are_not_trimmed_twice():
    s = _stt()
    s.transcribe(np.zeros(16000, dtype=np.int16))
    _, kwargs = s.model.calls[0]
    assert kwargs["vad_filter"] is False


def test_vad_filter_can_be_turned_back_on():
    s = _stt(vad_filter=True)
    s.transcribe(np.zeros(16000, dtype=np.int16))
    assert s.model.calls[0][1]["vad_filter"] is True


def test_a_blank_prompt_is_sent_as_none_not_empty_string():
    """faster-whisper treats '' as a real (empty) prompt; None means 'no prompt'."""
    s = _stt(initial_prompt="")
    s.transcribe(np.zeros(16000, dtype=np.int16))
    assert s.model.calls[0][1]["initial_prompt"] is None


def test_audio_is_scaled_to_float32_in_minus_one_to_one():
    s = _stt()
    pcm = np.array([-32768, 0, 32767], dtype=np.int16)
    s.transcribe(pcm)
    audio, _ = s.model.calls[0]
    assert audio.dtype == np.float32
    assert audio[0] == pytest.approx(-1.0, abs=1e-4)
    assert audio[2] == pytest.approx(1.0, abs=1e-4)


def test_condition_on_previous_text_stays_off():
    """Each capture is a standalone utterance; carrying context over invites
    a hallucinated segment to seed the next one."""
    s = _stt()
    s.transcribe(np.zeros(16000, dtype=np.int16))
    assert s.model.calls[0][1]["condition_on_previous_text"] is False


def test_confidence_gates_and_hallucination_filter_still_apply():
    noise = FakeModel([Seg("thanks for watching")])
    assert _stt(noise).transcribe(np.zeros(16000, dtype=np.int16)) == ""

    unsure = FakeModel([Seg("lights on", no_speech_prob=0.99)])
    assert _stt(unsure).transcribe(np.zeros(16000, dtype=np.int16)) == ""

    good = FakeModel([Seg(" lights on ")])
    assert _stt(good).transcribe(np.zeros(16000, dtype=np.int16)) == "lights on"
