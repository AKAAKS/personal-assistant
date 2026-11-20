"""The server decides whether a turn is finished and ships that on the reply as
an additive "turn" key: open | closed | voided_open | voided_closed."""

import asyncio
import json

import numpy as np

from jarvisd.server import JarvisServer
from tests.test_server import FakeTTS, FakeWS
from voice.stt import classify_text


class ScriptedAssistant:
    def __init__(self, says="Done.", awaiting=False):
        self.says = says
        self.awaiting_confirmation = awaiting
        self.conversation = []
        self.calls = []

    def process(self, text):
        self.calls.append(text)
        return self.says


class ScriptedSTT:
    def __init__(self, heard):
        self.heard = heard

    def transcribe_classified(self, audio):
        return self.heard, classify_text(self.heard)


class Notifier:
    def __init__(self, notices):
        self.notices = notices

    def drain(self):
        return self.notices


PCM = np.zeros(1600, dtype=np.int16).tobytes()


def utter(server, heard):
    """Feed one utterance through _handle_audio; return the decoded reply frame."""
    server.stt = ScriptedSTT(heard)
    ws = FakeWS()
    asyncio.run(server._handle_audio(ws, PCM))
    return json.loads(ws.sent[0])


def make(says="Done.", awaiting=False, notifier=None):
    return JarvisServer(ScriptedAssistant(says, awaiting), stt=None, tts=None,
                        notifier=notifier)


def test_a_completed_action_closes_the_mic():
    assert utter(make("Done."), "turn the lights off")["turn"] == "closed"


def test_a_question_holds_the_mic_open():
    reply = utter(make("Which one, the 3pm or the 4pm?"), "delete my meeting")
    assert reply["turn"] == "open"


def test_a_confirmation_request_holds_the_mic_open():
    server = make("That will delete it permanently. Confirm.", awaiting=True)
    assert utter(server, "delete the 3pm meeting")["turn"] == "open"


def test_a_question_still_holds_open_when_a_notice_is_appended():
    """_append_drained_notices ends the text in a period, so the flag must be
    computed on what _process returned."""
    server = make("Which one?", notifier=Notifier(["a timer finished"]))
    reply = utter(server, "delete my meeting")
    assert reply["text"].endswith("a timer finished.")
    assert reply["turn"] == "open"


def test_a_concluder_closes_without_calling_the_agent():
    server = make()
    reply = utter(server, "that's it")
    assert reply["turn"] == "closed"
    assert reply["text"] == "" and reply["heard"] == "that's it"
    assert reply["audio_follows"] is False
    assert server.assistant.calls == []


def test_a_concluder_is_ignored_right_after_an_open_turn():
    """'that's it' is a plausible answer to a destructive-action prompt;
    swallowing it would close the mic instead of letting the user answer."""
    server = make("Delete it permanently?")
    ws = FakeWS()
    server.stt = ScriptedSTT("delete the meeting")
    asyncio.run(server._handle_audio(ws, PCM))
    assert json.loads(ws.sent[0])["turn"] == "open"

    server.assistant.says = "Okay."
    server.stt = ScriptedSTT("that's it")
    asyncio.run(server._handle_audio(ws, PCM))
    reply = json.loads(ws.sent[1])
    assert reply["turn"] == "closed" and reply["text"] == "Okay."
    assert server.assistant.calls == ["delete the meeting", "that's it"]


def test_the_open_exemption_lasts_one_turn_only():
    server = make("Sure?")
    ws = FakeWS()
    for heard in ("do a thing", "that's it", "that's it"):
        server.stt = ScriptedSTT(heard)
        server.assistant.says = "Sure?" if heard == "do a thing" else "Okay."
        asyncio.run(server._handle_audio(ws, PCM))
    frames = [json.loads(m) for m in ws.sent]
    assert frames[1]["text"] == "Okay."          # exempted, reached the agent
    assert frames[2]["text"] == "" and frames[2]["turn"] == "closed"  # honoured
    assert server.assistant.calls == ["do a thing", "that's it"]


def test_never_mind_voids_and_keeps_the_mic_open():
    server = make()
    reply = utter(server, "remind me at five, never mind")
    assert reply["turn"] == "voided_open" and reply["text"] == ""
    assert server.assistant.calls == []


def test_forget_it_voids_and_closes():
    reply = utter(make(), "remind me at five, forget it")
    assert reply["turn"] == "voided_closed"


def test_an_unverified_frame_is_unchanged():
    class RejectAll:
        def accepts(self, audio):
            return False

    server = JarvisServer(ScriptedAssistant(), stt=ScriptedSTT("x"), tts=FakeTTS(),
                          speaker_verifier=RejectAll())
    ws = FakeWS()
    asyncio.run(server._handle_audio(ws, PCM))
    reply = json.loads(ws.sent[0])
    assert reply["reason"] == "unverified"
    assert "turn" not in reply


def test_an_empty_transcription_frame_carries_no_turn():
    reply = utter(make(), "   ")
    assert "turn" not in reply


def test_text_in_carries_the_turn_too():
    server = make("Which one?")
    ws = FakeWS()
    asyncio.run(server._handle_json(ws, json.dumps({"type": "text_in", "text": "hi"})))
    assert json.loads(ws.sent[0])["turn"] == "open"


def test_an_unverified_frame_between_a_question_and_its_answer_keeps_the_exemption():
    """JARVIS's own TTS echo is rejected as 'unverified' between the prompt and
    the user's answer; it must not clear the concluder exemption."""
    class RejectOnce:
        def __init__(self):
            self.reject = False

        def accepts(self, audio):
            ok = not self.reject
            self.reject = False
            return ok

    verifier = RejectOnce()
    server = make("Delete it permanently?")
    server.speaker_verifier = verifier
    ws = FakeWS()
    server.stt = ScriptedSTT("delete the meeting")
    asyncio.run(server._handle_audio(ws, PCM))
    assert json.loads(ws.sent[0])["turn"] == "open"

    verifier.reject = True
    asyncio.run(server._handle_audio(ws, PCM))
    assert json.loads(ws.sent[1])["reason"] == "unverified"

    server.assistant.says = "Okay."
    server.stt = ScriptedSTT("that's it")
    asyncio.run(server._handle_audio(ws, PCM))
    assert json.loads(ws.sent[2])["text"] == "Okay."
    assert server.assistant.calls == ["delete the meeting", "that's it"]


def test_an_empty_transcription_between_a_question_and_its_answer_keeps_the_exemption():
    server = make("Delete it permanently?")
    ws = FakeWS()
    server.stt = ScriptedSTT("delete the meeting")
    asyncio.run(server._handle_audio(ws, PCM))
    server.stt = ScriptedSTT("   ")
    asyncio.run(server._handle_audio(ws, PCM))
    server.assistant.says = "Okay."
    server.stt = ScriptedSTT("that's it")
    asyncio.run(server._handle_audio(ws, PCM))
    assert server.assistant.calls == ["delete the meeting", "that's it"]


def test_reset_clears_the_open_memory_so_a_concluder_is_honoured():
    server = make("Delete it permanently?")
    ws = FakeWS()
    server.stt = ScriptedSTT("delete the meeting")
    asyncio.run(server._handle_audio(ws, PCM))
    server.assistant.reset_conversation = lambda: None
    asyncio.run(server._handle_json(ws, json.dumps({"type": "reset"})))
    server.stt = ScriptedSTT("that's it")
    asyncio.run(server._handle_audio(ws, PCM))
    reply = json.loads(ws.sent[-1])
    assert reply["turn"] == "closed" and reply["text"] == ""
    assert server.assistant.calls == ["delete the meeting"]


def test_never_mind_after_an_open_turn_still_voids():
    """The one-turn exemption covers CONCLUDERS only. "that's it" is a
    plausible answer to "delete it permanently?"; "never mind" is the user
    cancelling -- the exact case the void feature exists for -- so suppressing
    it would shut the mic on someone about to rephrase."""
    server = make("Delete it permanently?")
    ws = FakeWS()
    server.stt = ScriptedSTT("delete the meeting")
    asyncio.run(server._handle_audio(ws, PCM))
    assert json.loads(ws.sent[0])["turn"] == "open"

    server.stt = ScriptedSTT("never mind")
    asyncio.run(server._handle_audio(ws, PCM))
    reply = json.loads(ws.sent[1])
    assert reply["turn"] == "voided_open" and reply["text"] == ""
    assert server.assistant.calls == ["delete the meeting"]


def test_forget_it_after_an_open_turn_still_voids_and_closes():
    server = make("Delete it permanently?")
    ws = FakeWS()
    server.stt = ScriptedSTT("delete the meeting")
    asyncio.run(server._handle_audio(ws, PCM))
    assert json.loads(ws.sent[0])["turn"] == "open"

    server.stt = ScriptedSTT("forget it")
    asyncio.run(server._handle_audio(ws, PCM))
    reply = json.loads(ws.sent[1])
    assert reply["turn"] == "voided_closed" and reply["text"] == ""
    assert server.assistant.calls == ["delete the meeting"]
