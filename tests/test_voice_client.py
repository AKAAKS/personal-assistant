"""Voice client follow-up capture — the hands-free conversation continuation.

We build the client via __new__ so the wake-word model and real microphone are
never touched; only the pieces _capture_followup uses are faked.
"""

import io
import wave

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


# --- _record_utterance() (the record-after-wake half, shared with barge-in) ---


def test_record_utterance_returns_pcm_on_speech():
    audio = np.ones(16000, dtype=np.int16)
    c = _client(audio)
    c._wake_buf = np.array([], dtype=np.int16)
    out = c._record_utterance()
    assert out == audio.tobytes()
    assert c.mic.clears == 1


def test_record_utterance_returns_none_on_silence():
    c = _client(None)
    c._wake_buf = np.array([], dtype=np.int16)
    assert c._record_utterance() is None


def test_record_utterance_returns_none_on_tooshort_blip():
    c = _client(np.ones(100, dtype=np.int16))  # < 1600 samples -> ignored
    c._wake_buf = np.array([], dtype=np.int16)
    assert c._record_utterance() is None


def test_record_utterance_resets_wake_buf():
    c = _client(np.ones(16000, dtype=np.int16))
    c._wake_buf = np.array([1, 2, 3], dtype=np.int16)
    c._record_utterance()
    assert len(c._wake_buf) == 0


def test_record_utterance_uses_the_same_vad_params_as_capture_utterance():
    c = _client(np.ones(16000, dtype=np.int16))
    c._wake_buf = np.array([], dtype=np.int16)
    c._record_utterance()
    assert c.vad.calls[0] == {
        "max_seconds": 15.0,
        "silence_ms": 800,
        "speech_timeout_sec": 5.0,
    }


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


# --- conversation state machine (_followup_next) ------------------------------
# Regression guard: a noisy room trips the VAD every couple seconds, producing
# audio that transcribes to nothing. Without a misfire cap the client looped
# forever re-recording that noise. These pin the "eventually re-arm" behavior.

def test_productive_reply_keeps_conversation_and_clears_misfires():
    nxt = VoiceClient._followup_next(in_conversation=True, misfires=1, productive=True)
    assert nxt == (True, 0)


def test_productive_reply_starts_a_conversation():
    nxt = VoiceClient._followup_next(in_conversation=False, misfires=0, productive=True)
    assert nxt == (True, 0)


def test_misfires_accumulate_then_end_the_conversation():
    from clients import voice_client

    # Simulate the runaway: keep feeding empty transcriptions while in a
    # conversation and assert it terminates within MAX_FOLLOWUP_MISFIRES.
    in_conv, misfires = True, 0
    seen = 0
    while in_conv:
        in_conv, misfires = VoiceClient._followup_next(
            in_conversation=in_conv, misfires=misfires, productive=False
        )
        seen += 1
        assert seen <= voice_client.MAX_FOLLOWUP_MISFIRES  # must not loop forever
    assert seen == voice_client.MAX_FOLLOWUP_MISFIRES
    assert (in_conv, misfires) == (False, 0)


def test_misfire_outside_conversation_is_a_noop():
    # Not in a conversation (wake-word gated): an empty result must not flip us
    # into a conversation or start counting.
    nxt = VoiceClient._followup_next(in_conversation=False, misfires=0, productive=False)
    assert nxt == (False, 0)


# --- _wav_duration() helpers and tests ----------------------------------------


def _make_wav(seconds: float, rate: int = 16000) -> bytes:
    """Build a real WAV buffer with the stdlib wave module."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(np.zeros(int(seconds * rate), dtype=np.int16).tobytes())
    return buf.getvalue()


def test_wav_duration_reads_header():
    """_wav_duration should parse duration from valid WAV headers."""
    assert abs(VoiceClient._wav_duration(_make_wav(0.5)) - 0.5) < 0.01
    assert abs(VoiceClient._wav_duration(_make_wav(1.0, rate=22050)) - 1.0) < 0.01


def test_wav_duration_different_sample_rates():
    """_wav_duration should work correctly at different sample rates."""
    # Test 44100 Hz
    assert abs(VoiceClient._wav_duration(_make_wav(0.5, rate=44100)) - 0.5) < 0.01
    # Test 8000 Hz
    assert abs(VoiceClient._wav_duration(_make_wav(2.0, rate=8000)) - 2.0) < 0.01
    # Test 48000 Hz
    assert abs(VoiceClient._wav_duration(_make_wav(0.25, rate=48000)) - 0.25) < 0.01


def test_wav_duration_bad_bytes_is_zero():
    """_wav_duration should return 0.0 for non-WAV bytes."""
    assert VoiceClient._wav_duration(b"not a wav") == 0.0
    assert VoiceClient._wav_duration(b"") == 0.0
    assert VoiceClient._wav_duration(b"\x00\x00\x00\x00") == 0.0


def test_wav_duration_empty_wav_is_zero():
    """_wav_duration should return 0.0 for an empty WAV file."""
    assert VoiceClient._wav_duration(_make_wav(0.0)) == 0.0


def test_wav_duration_zero_framerate_returns_zero_no_crash():
    """Regression: _wav_duration should guard against framerate=0 and not crash.

    A WAV header declaring framerate=0 would cause ZeroDivisionError when
    computing duration without the 'if rate else 0.0' guard. This test verifies
    the guard is in place and prevents the crash.
    """
    # Build a WAV buffer with framerate=0 by patching the header
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)  # Start with valid rate
        w.writeframes(np.zeros(16000, dtype=np.int16).tobytes())

    wav_bytes = bytearray(buf.getvalue())
    # Sample rate is at byte offset 24 (4 bytes, little-endian)
    wav_bytes[24:28] = b'\x00\x00\x00\x00'

    # Verify wave module parses it with framerate=0
    with wave.open(io.BytesIO(bytes(wav_bytes)), "rb") as w:
        assert w.getframerate() == 0, "Failed to construct framerate=0 WAV"

    # Test should not raise ZeroDivisionError and should return 0.0
    assert VoiceClient._wav_duration(bytes(wav_bytes)) == 0.0
