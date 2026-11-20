"""Classifying a transcript: concluder, void, noise or normal.

classify_text is pure -- no model, no audio -- so it is tested like
_is_hallucination. The apostrophe cases matter most: the phrase lists are stored
pre-normalised ("that s it"), and a list written with apostrophes would match
nothing while looking exactly like the old behaviour.
"""

import numpy as np
import pytest

from voice.stt import (
    INTENT_CONCLUDE,
    INTENT_NOISE,
    INTENT_NORMAL,
    INTENT_VOID_CLOSE,
    INTENT_VOID_CONTINUE,
    SpeechToText,
    classify_text,
)

AUDIO = np.zeros(1600, dtype=np.int16)


class _Seg:
    def __init__(self, text):
        self.text = text
        self.no_speech_prob = 0.0
        self.avg_logprob = 0.0


class _FakeModel:
    def __init__(self, text):
        self.text = text

    def transcribe(self, audio, **kwargs):
        return iter([_Seg(self.text)]), None


def stub_stt(returns):
    s = SpeechToText.__new__(SpeechToText)  # skip __init__: no model download
    s.model = _FakeModel(returns)
    s.initial_prompt = ""
    s.vad_filter = False
    s.beam_size = 1
    return s


# Concluders: the phrase is the WHOLE utterance. Apostrophe forms are the proof
# that the pre-normalised lists actually match.
@pytest.mark.parametrize("text", [
    "That's it.", "that's all", "Thank you.", "thanks", "We're done.", "thats it",
])
def test_whole_utterance_concluders(text):
    assert classify_text(text) == INTENT_CONCLUDE


# Void phrases: anchored to the END of the utterance.
def test_void_continue_at_end_of_a_real_command():
    assert classify_text("remind me at five, never mind") == INTENT_VOID_CONTINUE


@pytest.mark.parametrize("text", ["scratch that", "disregard that", "Never mind."])
def test_void_continue_phrases(text):
    assert classify_text(text) == INTENT_VOID_CONTINUE


def test_void_close_closes_the_mic():
    assert classify_text("remind me at five, forget it") == INTENT_VOID_CLOSE
    assert classify_text("Forget it.") == INTENT_VOID_CLOSE


# The safety property: anchoring to the end.
def test_a_void_phrase_at_the_START_is_a_normal_command():
    assert classify_text("never mind that, what's the weather") == INTENT_NORMAL


def test_a_void_phrase_must_end_on_a_word_boundary():
    assert classify_text("play the song forget its name") == INTENT_NORMAL


def test_a_concluder_inside_a_real_command_is_normal():
    """The trap that killed trailing concluders: an ends-with rule would
    strip this one and leave 'remind me to say'."""
    assert classify_text("remind me to say thank you") == INTENT_NORMAL


def test_noise_is_still_noise():
    assert classify_text("see you in the next video") == INTENT_NOISE
    assert classify_text("hmm") == INTENT_NOISE
    assert classify_text("") == INTENT_NOISE


def test_a_spoken_confirmation_is_never_a_concluder():
    for text in ("yes", "no", "okay", "yeah", "sure"):
        assert classify_text(text) == INTENT_NORMAL


def test_transcribe_returns_text_only_for_normal_intent():
    stt = stub_stt(returns="that's it")
    assert stt.transcribe(AUDIO) == ""
    assert stt.transcribe_classified(AUDIO) == ("that's it", INTENT_CONCLUDE)


def test_transcribe_still_returns_a_normal_command():
    stt = stub_stt(returns="lights on")
    assert stt.transcribe(AUDIO) == "lights on"
    assert stt.transcribe_classified(AUDIO) == ("lights on", INTENT_NORMAL)


def test_void_close_wins_a_tie_with_void_continue():
    assert classify_text("never mind, forget it") == INTENT_VOID_CLOSE


@pytest.mark.parametrize("text", ["thank you very much", "thanks a lot"])
def test_unapproved_politeness_phrases_stay_noise(text):
    assert classify_text(text) == INTENT_NOISE
