"""Whisper segment confidence filter — drops hallucinated text on noise/silence.

faster-whisper returns per-segment no_speech_prob / avg_logprob; on noise or the
tail of JARVIS's own voice it emits a low-confidence segment of invented words
(e.g. "and an abrogational postform"). _confident_text keeps only segments the
model is actually confident are speech, so noise becomes "" (no reply) instead of
gibberish the model then answers.
"""

from dataclasses import dataclass

from voice.stt import _confident_text


@dataclass
class Seg:
    text: str
    no_speech_prob: float = 0.1
    avg_logprob: float = -0.3


def test_keeps_confident_speech():
    segs = [Seg("What's the weather", no_speech_prob=0.05, avg_logprob=-0.25)]
    assert _confident_text(segs) == "What's the weather"


def test_drops_hallucination_with_high_no_speech_prob():
    segs = [Seg("and an abrogational postform", no_speech_prob=0.92, avg_logprob=-0.4)]
    assert _confident_text(segs) == ""


def test_drops_low_logprob_garbage():
    segs = [Seg("thanks for watching", no_speech_prob=0.3, avg_logprob=-1.8)]
    assert _confident_text(segs) == ""


def test_keeps_only_confident_segments_when_mixed():
    segs = [
        Seg("turn on the lights", no_speech_prob=0.1, avg_logprob=-0.4),
        Seg("subscribe now", no_speech_prob=0.95, avg_logprob=-0.5),  # hallucination
    ]
    assert _confident_text(segs) == "turn on the lights"


def test_empty_segments_give_empty_string():
    assert _confident_text([]) == ""
