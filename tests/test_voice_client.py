"""Voice client follow-up capture — the hands-free conversation continuation.

We build the client via __new__ so the wake-word model and real microphone are
never touched; only the pieces _capture_followup uses are faked.
"""

import asyncio
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


# --- the mic has exactly one owner at a time -------------------------------
#
# A proactive announcement arrives on the BACKGROUND RECEIVER task
# (on_proactive_audio=self._play) while the main loop is blocked in a capture on
# a different worker thread. _play then ran _drain_mic concurrently with
# _capture_utterance / _capture_followup, and the two share all the mic state:
# _drain_mic consumes frames the capture is waiting for, calls self.mic.clear()
# which flushes the queue mid-capture, and reassigns self._wake_buf while
# _capture_utterance is doing self._wake_buf = np.concatenate([...]) -- a lost
# update that drops or re-feeds wake-word chunks.
#
# The old _play_lock only serialized PlaySound against PlaySound, so it could
# not help: the capture never took it.
#
# Decision (product, not mechanical): a reminder WAITS for the capture to
# finish rather than cutting the user off mid-sentence. Losing a command is
# worse than a ~2s delay on an announcement, and JARVIS talking over the user
# is the same class of problem as the feedback loop this client exists to stop.
# One lock now guards both devices, so "who owns the mic" has a single answer.


def test_a_proactive_announcement_waits_for_an_active_capture(monkeypatch):
    import threading

    monkeypatch.setattr(winsound, "PlaySound", lambda *a: None)

    async def scenario():
        c = _client(None)
        c._audio_lock = asyncio.Lock()
        events = []
        capture_running = threading.Event()
        let_capture_finish = threading.Event()

        def fake_capture():
            events.append("capture:enter")
            capture_running.set()
            let_capture_finish.wait(5.0)
            events.append("capture:exit")
            return b"pcm"

        monkeypatch.setattr(c, "_drain_mic", lambda *a, **k: events.append("drain"))

        async def capture_turn():
            # what run() does for every capture
            async with c._audio_lock:
                return await asyncio.to_thread(fake_capture)

        cap = asyncio.create_task(capture_turn())
        await asyncio.to_thread(capture_running.wait, 5.0)

        play = asyncio.create_task(c._play(b"wav"))
        await asyncio.sleep(0.1)   # ample time to misbehave if unguarded

        assert "drain" not in events, (
            "the proactive announcement drained the mic while a capture was "
            "in flight; it would eat the user's speech and race _wake_buf"
        )

        let_capture_finish.set()
        await asyncio.gather(cap, play)
        assert events.index("capture:exit") < events.index("drain")

    asyncio.run(scenario())


def _run_ast():
    """VoiceClient.run as an AST, for exact nesting questions.

    Indentation heuristics cannot answer these: a capture's `async with` sits at
    a SHALLOWER indent than the reply playback yet does not enclose it, because
    the block closed. Containment is a tree question, so ask the tree.
    """
    import ast
    import inspect
    import textwrap

    return ast.parse(textwrap.dedent(inspect.getsource(VoiceClient.run)))


def _audio_lock_blocks(tree):
    import ast

    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncWith) and any(
            "_audio_lock" in ast.dump(item.context_expr) for item in node.items
        ):
            out.append(node)
    return out


def _calls_to(node, attr):
    import ast

    return [
        n for n in ast.walk(node)
        if isinstance(n, ast.Attribute) and n.attr == attr
    ]


def test_the_main_loop_holds_the_audio_lock_around_every_bounded_capture():
    """The concurrency test above can only prove _play cooperates. This pins the
    other half: run() must actually take the lock around the captures it owns.

    The idle wake-word wait is deliberately NOT in this list -- it is unbounded,
    so it manages the lock in slices inside _await_wake_utterance. The test
    below pins that path.
    """
    tree = _run_ast()
    blocks = _audio_lock_blocks(tree)
    assert blocks, "run() no longer acquires _audio_lock at all"

    for fn in ("_record_utterance", "_capture_followup"):
        assert _calls_to(tree, fn), f"{fn} is no longer called by run(); update this test"
        guarded = any(_calls_to(b, fn) for b in blocks)
        assert guarded, (
            f"{fn} runs without holding _audio_lock, so a proactive "
            "announcement can drain the mic underneath it"
        )


def test_the_idle_branch_goes_through_the_slicing_wait():
    """If run() ever calls _capture_utterance directly again -- inside the lock
    or outside it -- the reminder bug is back: inside, announcements are
    suppressed until the user speaks; outside, a proactive drains the mic
    mid-capture."""
    tree = _run_ast()
    assert _calls_to(tree, "_await_wake_utterance"), (
        "run() no longer uses the slicing idle wait"
    )
    assert not _calls_to(tree, "_capture_utterance"), (
        "run() calls _capture_utterance directly; the idle wait must slice"
    )


def test_the_slicing_wait_scans_under_the_audio_lock():
    """Structural half of the mid-slice exclusivity test above."""
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(VoiceClient._await_wake_utterance)))
    blocks = _audio_lock_blocks(tree)
    assert blocks, "_await_wake_utterance no longer takes the audio lock"
    assert any(_calls_to(b, "_scan_for_wake_and_record") for b in blocks), (
        "the scan slice runs without the audio lock, so a proactive can drain "
        "the mic while the wake word is being listened for"
    )


def test_the_reply_is_not_played_while_the_audio_lock_is_held():
    """asyncio.Lock is NOT reentrant, so widening a capture's `async with` to
    cover the reply would deadlock the client outright: _play_and_watch_reply
    acquires the same lock.
    """
    tree = _run_ast()
    assert _calls_to(tree, "_play_and_watch_reply"), "reply playback moved"
    for b in _audio_lock_blocks(tree):
        assert not _calls_to(b, "_play_and_watch_reply"), (
            "a capture holds _audio_lock across the reply playback; "
            "_play_and_watch_reply re-acquires it and the client deadlocks"
        )


# --- the idle wake-word wait must not hold the mic hostage ------------------
#
# The ownership lock above is right for a BOUNDED capture: a follow-up or a
# post-barge-in recording lasts seconds, and "a reminder waits a couple of
# seconds rather than cutting the user off" is the correct trade.
#
# The idle wake-word wait is not bounded. It ends only when the user says "hey
# jarvis", so holding the same lock across it meant a reminder coming due while
# JARVIS sat idle -- the normal case for every reminder and timer -- waited for
# the user to speak first. That is exactly the reported bug: the announcement
# only ever arrived as "this passed while you were away" on the next turn.
#
# The fix keeps mic ownership exclusive but makes the idle wait yield: it scans
# in short slices, each under the lock, releasing between them. A proactive
# gets the mic within one slice instead of never.


def _wake_wait_client(monkeypatch, scan):
    c = VoiceClient.__new__(VoiceClient)
    c.mic = FakeMic()
    c._audio_lock = asyncio.Lock()
    monkeypatch.setattr(c, "_scan_for_wake_and_record", scan)
    return c


def test_a_proactive_announcement_does_not_wait_for_the_idle_wake_word_wait(monkeypatch):
    """The bug, pinned: a reminder arriving while idle must be spoken now."""
    import threading

    monkeypatch.setattr(winsound, "PlaySound", lambda *a: None)

    async def scenario():
        events = []
        scanning = threading.Event()

        def fake_scan(slice_sec):
            scanning.set()
            events.append("scan")
            time.sleep(0.02)  # a slice that hears nothing
            return None, False

        c = _wake_wait_client(monkeypatch, fake_scan)
        monkeypatch.setattr(c, "_drain_mic", lambda *a, **k: events.append("drain"))

        wait = asyncio.create_task(c._await_wake_utterance())
        await asyncio.to_thread(scanning.wait, 5.0)
        try:
            # The user never speaks, so the wait is still running. The
            # announcement must still get the mic.
            await asyncio.wait_for(c._play(b"wav"), timeout=3.0)
        finally:
            wait.cancel()
        assert "drain" in events, "the announcement never played"

    asyncio.run(scenario())


def test_the_idle_wait_holds_the_lock_while_a_slice_is_actually_scanning(monkeypatch):
    """Yielding between slices must not cost mic exclusivity DURING one: a
    drain mid-slice still eats frames and races _wake_buf."""
    import threading

    monkeypatch.setattr(winsound, "PlaySound", lambda *a: None)

    async def scenario():
        events = []
        in_slice = threading.Event()
        let_slice_finish = threading.Event()

        def fake_scan(slice_sec):
            events.append("slice:enter")
            in_slice.set()
            let_slice_finish.wait(5.0)
            events.append("slice:exit")
            return None, False

        c = _wake_wait_client(monkeypatch, fake_scan)
        monkeypatch.setattr(c, "_drain_mic", lambda *a, **k: events.append("drain"))

        wait = asyncio.create_task(c._await_wake_utterance())
        await asyncio.to_thread(in_slice.wait, 5.0)
        play = asyncio.create_task(c._play(b"wav"))
        await asyncio.sleep(0.1)  # ample time to misbehave if unguarded
        assert "drain" not in events, "a proactive drained the mic mid-slice"
        let_slice_finish.set()
        await asyncio.wait_for(play, timeout=3.0)
        wait.cancel()
        assert events.index("slice:exit") < events.index("drain")

    asyncio.run(scenario())


def test_the_idle_wait_returns_the_pcm_from_the_slice_that_fired(monkeypatch):
    async def scenario():
        calls = []

        def fake_scan(slice_sec):
            calls.append(slice_sec)
            if len(calls) < 3:
                return None, False
            return b"pcm", True

        c = _wake_wait_client(monkeypatch, fake_scan)
        assert await c._await_wake_utterance() == b"pcm"
        assert len(calls) == 3, "it stopped slicing before the wake word fired"

    asyncio.run(scenario())


def test_a_fired_slice_that_captured_nothing_still_ends_the_wait(monkeypatch):
    """A wake word followed by silence returns None, and run() re-arms on it.
    Treating that as "keep slicing" would swallow the re-arm beep."""
    async def scenario():
        c = _wake_wait_client(monkeypatch, lambda s: (None, True))
        assert await c._await_wake_utterance() is None

    asyncio.run(scenario())


# --- _scan_for_wake_and_record: one bounded slice --------------------------


def test_a_slice_that_hears_nothing_reports_not_fired():
    c = _watch_client(FakeWake(fire_on=0))  # never fires
    pcm, fired = c._scan_for_wake_and_record(0.05)
    assert (pcm, fired) == (None, False)
    assert c.mic.reads > 0, "the slice never read the mic"


def test_a_slice_that_hears_the_wake_word_records_and_reports_fired(monkeypatch):
    c = _watch_client(FakeWake(fire_on=1))
    monkeypatch.setattr(c, "_record_utterance", lambda: b"pcm")
    assert c._scan_for_wake_and_record(5.0) == (b"pcm", True)


def test_an_unbounded_slice_blocks_until_the_wake_word(monkeypatch):
    """slice_sec=None keeps _capture_utterance's original semantics."""
    c = _watch_client(FakeWake(fire_on=4))
    monkeypatch.setattr(c, "_record_utterance", lambda: b"pcm")
    assert c._scan_for_wake_and_record(None) == (b"pcm", True)
    assert c.wake_word.calls == 4


def test_capture_utterance_still_blocks_until_the_wake_word(monkeypatch):
    """The sync entry point is unchanged for every non-idle caller."""
    c = _watch_client(FakeWake(fire_on=3))
    monkeypatch.setattr(c, "_record_utterance", lambda: b"pcm")
    assert c._capture_utterance() == b"pcm"
