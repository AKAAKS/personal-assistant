"""Voice client follow-up capture — the hands-free conversation continuation.

We build the client via __new__ so the wake-word model and real microphone are
never touched; only the pieces _capture_followup uses are faked.
"""

import io
import time
import wave
import winsound

import numpy as np

import clients.voice_client as vc
from clients.voice_client import PlaybackWakePolicy, VoiceClient


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


# The two "too-short blip" tests that used to live here are gone with
# MIN_UTTERANCE_SAMPLES. They asserted a client-side length floor that the real
# VAD makes unreachable: both callers pass silence_ms=800, so a non-None return
# carries >= 10 speech frames + 26 trailing-silence frames = 17,280 samples,
# against a floor of 8,000. They only passed because the fake VAD here hands
# back a raw 100-sample array, which record_until_silence cannot do. "Too short
# to be a turn" is now decided once, in VAD-positive milliseconds, by the only
# code that can see which frames were speech -- see
# test_audio_io.test_a_blip_of_noise_with_trailing_silence_is_rejected.


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


# --- _watch_for_wake() interruptible playback --------------------------------


WAKE_CHUNK = 1280  # WAKE_WORD_CHUNK


class FrameMic:
    """Mic that always has a full wake-word chunk ready, at a given level."""
    def __init__(self, level=0):
        self.clears = 0
        self.reads = 0
        self.level = level

    def clear(self):
        self.clears += 1

    def read_frame(self, timeout=1.0):
        self.reads += 1
        return np.full(WAKE_CHUNK, self.level, dtype=np.int16)


class FakeWake:
    """score() is high for calls in [fire_on, fire_until] (fire_on=0 = never).

    fire_until models a TRANSIENT high score -- the realistic shape of both a
    real barge-in and a self-fire on JARVIS's own speech, where surrounding
    chunks score low and so teach the policy its echo floor.
    """
    def __init__(self, fire_on=0, fire_until=None):
        self.fire_on = fire_on
        self.fire_until = fire_until
        self.calls = 0
        self.resets = 0

    def score(self, chunk):
        self.calls += 1
        if not self.fire_on or self.calls < self.fire_on:
            return 0.0
        if self.fire_until is not None and self.calls > self.fire_until:
            return 0.0
        return 0.95

    def process_frame(self, chunk):
        return self.score(chunk) >= 0.5

    def reset(self):
        self.resets += 1


def _watch_client(wake, level=8000, policy=None):
    c = VoiceClient.__new__(VoiceClient)
    c.mic = FrameMic(level=level)
    c.wake_word = wake
    c._wake_buf = np.array([], dtype=np.int16)
    c._playback_policy = policy or PlaybackWakePolicy(
        threshold=0.7, consecutive=2, level_ratio=2.0, min_level=200.0
    )
    return c


def test_watch_for_wake_returns_true_when_word_fires():
    # Loud, confident and sustained: the user really did cut in.
    c = _watch_client(FakeWake(fire_on=2), level=8000)
    assert c._watch_for_wake(duration=5.0) is True
    assert c.mic.clears == 1
    assert c.wake_word.resets == 1


def test_watch_for_wake_returns_false_after_duration():
    c = _watch_client(FakeWake(fire_on=0))  # never fires
    start = time.monotonic()
    assert c._watch_for_wake(duration=0.05) is False
    assert time.monotonic() - start < 2.0  # exits near the deadline, doesn't hang


# --- Bluetooth latency: the watch must cover the whole physical playout ------


def test_watch_waits_past_the_clip_for_output_latency():
    """The reply is still coming out of the speaker after the WAV's own
    duration. Ending the watch there is what purged mid-sentence."""
    c = _watch_client(FakeWake(fire_on=0))
    start = time.monotonic()
    c._watch_for_wake(duration=0.2)
    elapsed = time.monotonic() - start
    assert elapsed >= 0.2 + vc.OUTPUT_LATENCY_SEC, (
        f"watch ended after {elapsed:.2f}s, before the clip had played out"
    )


def test_quiet_self_fire_during_playback_is_not_a_barge_in():
    """JARVIS's own voice can briefly score high, but it is no louder than the
    echo floor the surrounding chunks established, so it must not interrupt."""
    # Chunks 1-14 score 0 at level 400 and teach the floor; 15-18 score 0.95 at
    # that same level -- a transient self-fire, not someone talking over it.
    c = _watch_client(FakeWake(fire_on=15, fire_until=18), level=400)
    assert c._watch_for_wake(duration=0.5) is False


def test_a_loud_barge_in_still_wins_at_the_same_echo_level():
    """Same transient, but the chunk is loud: that is a person, not the echo.
    Guards against the gate above being so strict it kills real barge-in."""
    c = _watch_client(FakeWake(fire_on=15, fire_until=25), level=400)
    # Hand the policy a loud candidate once the floor has settled on 400.
    for _ in range(20):
        c._playback_policy.observe(score=0.0, level=400.0)
    assert c._playback_policy.observe(score=0.95, level=6000.0) is False  # 1 of 2
    assert c._playback_policy.observe(score=0.95, level=6000.0) is True


# --- the echo drain ---------------------------------------------------------


def test_drain_mic_consumes_frames_for_the_window_then_clears():
    c = _watch_client(FakeWake(fire_on=0))
    c.mic.reads = 0
    start = time.monotonic()
    c._drain_mic(0.15)
    assert time.monotonic() - start >= 0.15
    assert c.mic.reads > 0, "drain must actually read (and discard) frames"
    assert c.mic.clears == 1


def test_drain_mic_is_a_noop_for_a_zero_window():
    c = _watch_client(FakeWake(fire_on=0))
    c.mic.reads = 0
    c._drain_mic(0)
    assert c.mic.reads == 0


# --- _play_and_watch() must actually run the watch ---------------------------
# Regression: winsound cannot play SND_MEMORY|SND_ASYNC at all (Windows raises
# RuntimeError: "Cannot play asynchronously from memory"). The original
# _play_and_watch caught that and fell back to a BLOCKING play that never ran
# the wake-word watch, so barge-in could never fire on a real machine.


def _async_from_memory_unsupported(monkeypatch):
    """Patch voice_client.winsound to mimic real Windows: async-from-memory
    raises, every other PlaySound call is recorded. Returns the recorded flags."""
    from clients import voice_client

    recorded = []

    def fake_playsound(data, flags):
        if (flags & winsound.SND_MEMORY) and (flags & winsound.SND_ASYNC):
            raise RuntimeError("Cannot play asynchronously from memory")
        recorded.append(flags)

    monkeypatch.setattr(voice_client.winsound, "PlaySound", fake_playsound)
    return recorded


def _valid_wav(seconds: float = 0.1, rate: int = 24000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(np.zeros(int(rate * seconds), dtype=np.int16).tobytes())
    return buf.getvalue()


def test_play_and_watch_runs_the_watch_despite_async_from_memory_limit(monkeypatch):
    """The barge-in window must run even though SND_MEMORY|SND_ASYNC is
    unsupported — the fix plays asynchronously another way, it does not skip
    the watch."""
    _async_from_memory_unsupported(monkeypatch)
    c = VoiceClient.__new__(VoiceClient)
    watched = {"called": False}

    def fake_watch(duration):
        watched["called"] = True
        return True

    c._watch_for_wake = fake_watch

    result = c._play_and_watch(_valid_wav())

    assert watched["called"] is True  # the barge-in watch actually ran
    assert result is True             # and its result is propagated back


# --- an unreadable WAV duration must not leak audio into the next capture ---
#
# `if interrupted or duration > 0` was added so an unparseable header would not
# cause a purge that truncates the reply. It traded one bug for a worse one:
# with duration == 0.0 and no barge-in, nothing purges, _watch_for_wake returns
# after only latency + tail (~0.6s), and the loop walks into _capture_followup
# while the clip is STILL PLAYING -- recording JARVIS as the user's follow-up,
# which is the feedback loop this whole branch exists to close. The 250ms
# _drain_mic cannot cover a multi-second reply.
#
# A clip whose length we cannot determine goes down the BLOCKING path instead:
# PlaySound returns only when playback is finished, so there is nothing left to
# purge and nothing left to bleed. Barge-in is lost for that one malformed clip,
# which is the right trade.


def test_an_unreadable_duration_plays_blocking_instead_of_watching(monkeypatch):
    calls = []

    def fake_play(target, flags):
        calls.append(flags)

    monkeypatch.setattr(winsound, "PlaySound", fake_play)
    c = _client(None)
    watched = []
    monkeypatch.setattr(c, "_watch_for_wake", lambda d: watched.append(d) or False)
    monkeypatch.setattr(c, "_drain_mic", lambda *a, **k: None)

    interrupted = c._play_and_watch(b"not a parseable wav")

    assert interrupted is False
    assert watched == [], (
        "the async watch ran for a clip whose end time is unknown; the capture "
        "that follows will record the still-playing reply"
    )
    assert winsound.SND_MEMORY in calls, "expected the blocking memory playback"
    assert not any(f & winsound.SND_ASYNC for f in calls), (
        "async playback was started for an unbounded clip"
    )


def test_a_readable_duration_still_takes_the_async_barge_in_path(monkeypatch):
    monkeypatch.setattr(winsound, "PlaySound", lambda *a: None)
    c = _client(None)
    watched = []
    monkeypatch.setattr(c, "_watch_for_wake", lambda d: watched.append(d) or False)
    monkeypatch.setattr(c, "_drain_mic", lambda *a, **k: None)

    c._play_and_watch(_make_wav(0.5))

    assert watched and abs(watched[0] - 0.5) < 0.01, "barge-in watch was skipped"


# --- a barge-in means an exchange IS in progress ---------------------------
#
# `pending_barge_in` is only ever set from inside the playback watch, so it
# implies a reply was sounding and therefore that an exchange is underway. The
# barge-in branch never set `in_conversation`, so on the FIRST exchange it
# stayed False -- and an `unverified` capture (JARVIS's own echo, the case the
# unverified branch exists for) then hit `not in_conversation` and rearmed,
# ending the session. Exactly the "hearing itself ends the conversation"
# behaviour the comment there says is fixed; it only worked from the second
# exchange on.


def test_an_unverified_echo_does_not_end_a_conversation_it_started_in():
    """_followup_next is the owner of this decision; the bug was the loop's
    `not in_conversation` short-circuit around it, which this pins."""
    # Mid-conversation: an unverified capture must NOT end the exchange.
    assert VoiceClient._followup_next(
        in_conversation=True, misfires=0, productive=False,
    ) == (True, 1)


def test_the_barge_in_branch_marks_the_conversation_active():
    """Static guarantee: the branch that consumes pending_barge_in sets
    in_conversation, so the unverified path cannot mistake a barge-in turn for
    an idle one."""
    import inspect

    src = inspect.getsource(VoiceClient.run)
    branch = src.split("if pending_barge_in:", 1)[1].split("elif in_conversation:", 1)[0]
    assert "in_conversation = True" in branch, (
        "the barge-in branch leaves in_conversation False, so an unverified "
        "echo on the first exchange rearms and ends the session"
    )
