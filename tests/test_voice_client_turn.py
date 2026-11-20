"""run() obeys the server's per-reply "turn" outcome.

run() is an endless async loop over a websocket, a mic and a wake word, so the
harness replaces each of those seams with a scripted fake and stops the loop by
raising a sentinel from the NEXT capture. Which capture it reaches is the
observable outcome: the follow-up capture means the window is still open
(in_conversation), the wake-word wait means it re-armed.
"""

import asyncio
import contextlib

import pytest

import clients.voice_client as vc
from clients.voice_client import VoiceClient


class _Stop(Exception):
    """Raised by the capture after the exchange under test to end the loop."""


class _State:
    def __init__(self):
        self.next_capture = None      # "followup" | "record" | "wake"
        self.followup_window = None   # window the follow-up capture was given
        self.beeps = 0                # the mallet == re-armed to the wake word
        self.interrupts = 0
        self.advances = []            # productive flags passed to _followup_next
        self.out = ""

    @property
    def in_conversation(self):
        return self.next_capture in ("followup", "record")

    @property
    def rearmed(self):
        return self.beeps > 0

    @property
    def pending_barge_in(self):
        return self.next_capture == "record"

    @property
    def printed_didnt_catch(self):
        return "didn't catch" in self.out


class _FakeWS:
    def __init__(self, state):
        self.state = state

    async def send(self, data):
        if isinstance(data, str) and "interrupt" in data:
            self.state.interrupts += 1

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def run_one_exchange(reply, *, interrupted=False, monkeypatch, capsys):
    state = _State()
    c = VoiceClient.__new__(VoiceClient)
    c.url = "ws://fake"
    c._audio_lock = asyncio.Lock()
    c.mic = type("M", (), {"start": lambda s: None, "stop": lambda s: None})()

    class FakeReceiver:
        def __init__(self, ws, **kw):
            pass

        async def run(self):
            await asyncio.sleep(3600)

        async def get_reply(self, task):
            fut = asyncio.get_running_loop().create_future()
            fut.set_result(b"wav")
            # Only a spoken reply has audio; a voided frame is silent.
            data = dict(reply, audio_follows=bool(reply.get("text")))
            return {"data": data, "wav_future": fut}

    calls = {"n": 0}

    async def wake():
        calls["n"] += 1
        if calls["n"] > 1:
            state.next_capture = "wake"
            raise _Stop
        return b"pcm"

    def followup(window=None):
        state.next_capture = "followup"
        state.followup_window = window
        raise _Stop

    def record():
        state.next_capture = "record"
        raise _Stop

    async def play(wav):
        return interrupted

    real_next = VoiceClient._followup_next

    def spy_next(**kw):
        state.advances.append(kw["productive"])
        return real_next(**kw)

    monkeypatch.setattr(vc.websockets, "connect", lambda *a, **k: _FakeWS(state))
    monkeypatch.setattr(vc, "ReceiveLoop", FakeReceiver)
    monkeypatch.setattr(vc.activity, "write", lambda *a, **k: None)
    monkeypatch.setattr(c, "_await_wake_utterance", wake)
    monkeypatch.setattr(c, "_capture_followup", followup)
    monkeypatch.setattr(c, "_record_utterance", record)
    monkeypatch.setattr(c, "_play_and_watch_reply", play)
    monkeypatch.setattr(VoiceClient, "_stopped_beep",
                        staticmethod(lambda: setattr(state, "beeps", state.beeps + 1)))
    monkeypatch.setattr(VoiceClient, "_followup_next", staticmethod(spy_next))

    with contextlib.suppress(_Stop):
        asyncio.run(c.run())
    state.out = capsys.readouterr().out
    return state


@pytest.fixture
def exchange(monkeypatch, capsys):
    return lambda reply, **kw: run_one_exchange(
        reply, monkeypatch=monkeypatch, capsys=capsys, **kw)


def test_closed_rearms_the_wake_word(exchange):
    state = exchange({"turn": "closed", "heard": "turn on the light", "text": "Done."})
    assert state.in_conversation is False
    assert state.rearmed is True
    assert state.next_capture == "wake"


def test_open_keeps_the_follow_up_window(exchange):
    state = exchange({"turn": "open", "heard": "play it", "text": "Which one?"})
    assert state.in_conversation is True
    assert state.rearmed is False
    assert state.followup_window == vc.FOLLOWUP_WINDOW_SEC


def test_a_missing_turn_field_behaves_exactly_as_today(exchange):
    """Additive field: an older server must not change client behaviour."""
    state = exchange({"heard": "hi", "text": "Done."})
    assert state.in_conversation is True
    assert state.rearmed is False
    assert state.advances == [True]


def test_voided_open_on_the_FIRST_utterance_reopens_the_window(exchange):
    """in_conversation is still False here, so 'reopen' must set it
    explicitly the way the barge-in branch does, or this drops to idle."""
    state = exchange({"turn": "voided_open", "heard": "never mind", "text": ""})
    assert state.in_conversation is True
    assert state.rearmed is False
    assert state.printed_didnt_catch is False
    assert state.advances == []  # misfires untouched: no productive/misfire step
    assert state.followup_window == vc.UTTERANCE_SPEECH_TIMEOUT_SEC


def test_voided_closed_rearms(exchange):
    state = exchange({"turn": "voided_closed", "heard": "forget it", "text": ""})
    assert state.in_conversation is False
    assert state.rearmed is True
    assert state.printed_didnt_catch is False


def test_a_barge_in_over_a_closing_reply_is_not_lost(exchange):
    """The close must be applied after _play_and_watch_reply returns
    not-interrupted, or the pending_barge_in branch is bypassed."""
    state = exchange({"turn": "closed", "heard": "x", "text": "Done."}, interrupted=True)
    assert state.pending_barge_in is True
    assert state.rearmed is False
    assert state.interrupts == 1
