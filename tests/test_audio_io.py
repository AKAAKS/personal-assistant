"""Pre-roll capture in record_until_silence.

WebRTC VAD needs a frame or two to latch onto a soft word onset. The frames it
spends deciding used to be thrown away, which clipped the attack of the first
word ("Hey Jarvis" transcribing as "service"). record_until_silence now keeps a
short rolling buffer of the frames it read before speech latched and prepends
them, so the onset survives.

Frames are faked end to end: no microphone, no webrtcvad.
"""

import numpy as np

from voice.audio_io import FRAME_SIZE, PRE_ROLL_MS, VoiceActivityDetector


class ScriptedMic:
    """Yields a fixed list of frames, then None forever (silence/timeout)."""

    def __init__(self, frames):
        self._frames = list(frames)
        self.clears = 0

    def clear(self):
        self.clears += 1

    def read_frame(self, timeout=1.0):
        return self._frames.pop(0) if self._frames else None


class ScriptedVAD(VoiceActivityDetector):
    """A VAD whose verdict is keyed off each frame's first sample.

    Non-zero sample -> speech. Lets a test spell out a speech/silence pattern
    as plain numbers without webrtcvad.
    """

    def __init__(self):
        pass  # deliberately skip webrtcvad construction

    def is_speech(self, frame):
        return bool(frame[0])


def _frame(value):
    return np.full(FRAME_SIZE, value, dtype=np.int16)


def _silence(n):
    return [_frame(0) for _ in range(n)]


def _speech(n, value=100):
    return [_frame(value) for _ in range(n)]


# --- the regression: onset frames must not be discarded ---------------------


def test_pre_roll_frames_are_prepended_when_speech_latches():
    """The two silent frames before speech are the clipped word onset."""
    vad = ScriptedVAD()
    # 2 "silent" (VAD-missed onset) frames, then 3 speech, then enough
    # silence to close the recording.
    mic = ScriptedMic(_silence(2) + _speech(3) + _silence(40))
    audio = vad.record_until_silence(mic, silence_ms=300, min_speech_ms=0)

    assert audio is not None
    frames = len(audio) // FRAME_SIZE
    # 2 pre-roll + 3 speech + 10 trailing silence (300ms / 30ms) = 15
    assert frames == 15, f"expected 15 frames, got {frames}"
    # The recording must START with the pre-roll, not with the speech.
    assert audio[0] == 0, "pre-roll frame missing: onset was clipped"
    assert audio[2 * FRAME_SIZE] == 100


def test_pre_roll_is_capped_at_the_window_length():
    """A long silence before speech contributes only PRE_ROLL_MS of it."""
    vad = ScriptedVAD()
    max_pre = PRE_ROLL_MS // 30
    mic = ScriptedMic(_silence(max_pre + 25) + _speech(2) + _silence(40))
    audio = vad.record_until_silence(
        mic, silence_ms=300, speech_timeout_sec=10.0, min_speech_ms=0
    )

    assert audio is not None
    frames = len(audio) // FRAME_SIZE
    assert frames == max_pre + 2 + 10


def test_pre_roll_can_be_disabled():
    vad = ScriptedVAD()
    mic = ScriptedMic(_silence(2) + _speech(3) + _silence(40))
    audio = vad.record_until_silence(
        mic, silence_ms=300, pre_roll_ms=0, min_speech_ms=0
    )

    assert audio is not None
    assert len(audio) // FRAME_SIZE == 13  # 3 speech + 10 trailing silence
    assert audio[0] == 100  # starts at the speech, as before


def test_explicit_pre_buffer_still_wins():
    """A caller-supplied pre_buffer means speech already started; the rolling
    pre-roll must not also fire and double up frames."""
    vad = ScriptedVAD()
    mic = ScriptedMic(_speech(2) + _silence(40))
    audio = vad.record_until_silence(
        mic, pre_buffer=_speech(1, value=55), silence_ms=300, min_speech_ms=0
    )

    assert audio is not None
    assert audio[0] == 55
    assert len(audio) // FRAME_SIZE == 1 + 2 + 10


# --- unchanged behaviour the pre-roll must not break ------------------------


def test_returns_none_when_speech_never_starts():
    vad = ScriptedVAD()
    mic = ScriptedMic(_silence(200))
    assert vad.record_until_silence(mic, speech_timeout_sec=1.0) is None


def test_returns_none_when_mic_yields_nothing():
    vad = ScriptedVAD()
    assert vad.record_until_silence(ScriptedMic([]), speech_timeout_sec=1.0) is None


# --- a capture must contain real SPEECH, not just a VAD blip ----------------
#
# Live log, 15:18: the follow-up window latched onto room noise twice and shipped
# 1.2s and 1.0s clips to the server, which scored them near zero against BOTH
# prints (noise, not a voice) and rejected them. Verification caught it, but the
# round trip was wasted and visible to the user. Length alone can't tell these
# apart: with silence_ms=800 of trailing silence appended, a 1.0s "utterance"
# holds only ~0.2s of actual speech.


def test_a_blip_of_noise_with_trailing_silence_is_rejected():
    vad = ScriptedVAD()
    # 2 speech frames (60ms) then silence: a VAD blip, not an utterance.
    mic = ScriptedMic(_speech(2) + _silence(40))
    assert vad.record_until_silence(mic, silence_ms=300, min_speech_ms=300) is None


def test_a_real_utterance_clears_the_speech_requirement():
    vad = ScriptedVAD()
    mic = ScriptedMic(_speech(15) + _silence(40))  # 450ms of speech
    audio = vad.record_until_silence(mic, silence_ms=300, min_speech_ms=300)
    assert audio is not None


def test_speech_is_counted_across_short_internal_pauses():
    """A natural pause mid-sentence must not reset the count -- only the total
    amount of speech matters."""
    vad = ScriptedVAD()
    mic = ScriptedMic(
        _speech(6) + _silence(3) + _speech(6) + _silence(40)  # 360ms total speech
    )
    audio = vad.record_until_silence(mic, silence_ms=300, min_speech_ms=300)
    assert audio is not None


def test_the_speech_requirement_can_be_disabled():
    vad = ScriptedVAD()
    mic = ScriptedMic(_speech(2) + _silence(40))
    assert vad.record_until_silence(mic, silence_ms=300, min_speech_ms=0) is not None


def test_an_explicit_pre_buffer_counts_toward_the_requirement():
    """The barge-in path hands in audio already known to be speech; it must not
    be penalised for arriving as a pre_buffer rather than through the VAD."""
    vad = ScriptedVAD()
    mic = ScriptedMic(_speech(2) + _silence(40))
    audio = vad.record_until_silence(
        mic, pre_buffer=_speech(12), silence_ms=300, min_speech_ms=300
    )
    assert audio is not None


def test_a_discarded_blip_does_not_end_the_capture_early():
    """A blip must not be reported as "nothing was said".

    In the follow-up branch a None return means the window closed in silence,
    which ENDS the hands-free conversation. If a 1s noise blip returned None,
    one cough would cut the user's thinking time from 6s to 1s. So a blip is
    discarded and the capture keeps waiting for real speech.
    """
    vad = ScriptedVAD()
    mic = ScriptedMic(
        _speech(2) + _silence(12)        # blip, then enough silence to close it
        + _speech(15) + _silence(40)     # then the user actually speaks
    )
    audio = vad.record_until_silence(
        mic, silence_ms=300, min_speech_ms=300, speech_timeout_sec=10.0
    )
    assert audio is not None
    # The kept audio is the real utterance, not the blip glued onto it.
    assert len(audio) // FRAME_SIZE <= 15 + 10 + PRE_ROLL_MS // 30


def test_persistent_noise_still_gives_up_within_the_patience_window():
    """Blips must not reset the clock forever, or a noisy room would hold the
    follow-up window open for the full max_seconds every single turn."""
    vad = ScriptedVAD()
    blips = (_speech(2) + _silence(12)) * 20   # nothing but blips
    mic = ScriptedMic(blips)
    audio = vad.record_until_silence(
        mic, silence_ms=300, min_speech_ms=300, speech_timeout_sec=1.0
    )
    assert audio is None
