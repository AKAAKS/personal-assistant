"""Voice client follow-up capture — the hands-free conversation continuation.

We build the client via __new__ so the wake-word model and real microphone are
never touched; only the pieces _capture_followup uses are faked.
"""

import numpy as np

from clients.voice_client import VoiceClient


class FakeMic:
    def __init__(self):
        self.clears = 0

    def clear(self):
        self.clears += 1


class FakeVAD:
    """Returns a canned recording; records the kwargs it was called with."""

    def __init__(self, result):
        self._result = result
        self.calls = []

    def record_until_silence(self, mic, **kwargs):
        self.calls.append(kwargs)
        return self._result


def _client(vad_result):
    c = VoiceClient.__new__(VoiceClient)  # skip __init__ (no wake model / mic)
    c.mic = FakeMic()
    c.vad = FakeVAD(vad_result)
    return c


def test_followup_returns_pcm_bytes_when_speech_captured():
    audio = np.ones(16000, dtype=np.int16)  # 1s of "speech"
    c = _client(audio)
    out = c._capture_followup()
    assert out == audio.tobytes()
    assert c.mic.clears == 1  # flushed audio captured while JARVIS was talking


def test_followup_returns_none_on_silence():
    c = _client(None)  # window passed with no speech
    assert c._capture_followup() is None


def test_followup_returns_none_on_tooshort_blip():
    c = _client(np.ones(100, dtype=np.int16))  # < 1600 samples -> ignored
    assert c._capture_followup() is None


def test_followup_uses_the_configured_window():
    from clients import voice_client

    c = _client(None)
    c._capture_followup()
    assert c.vad.calls[0]["speech_timeout_sec"] == voice_client.FOLLOWUP_WINDOW_SEC
