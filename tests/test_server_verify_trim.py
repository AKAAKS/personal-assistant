"""The speaker verifier is shown the clip with trailing silence trimmed; Whisper
still gets the whole clip. Silence dilutes the embedding (and the verifier's
duration rule counts it), but Whisper's own confidence gates handle silence
fine and the trailing context does it no harm."""

import asyncio

import numpy as np

from jarvisd.server import JarvisServer
from tests.test_server import ClassifyingSTT, FakeAssistant, FakeTTS, FakeWS


class RecordingVerifier:
    def __init__(self, seen):
        self.seen = seen

    def accepts(self, audio):
        self.seen["verify_len"] = len(audio)
        return True


class RecordingSTT(ClassifyingSTT):
    def __init__(self, seen):
        self.seen = seen

    def transcribe(self, audio):
        self.seen["stt_len"] = len(audio)
        return "turn on the light"


def test_verifier_sees_trimmed_audio_and_stt_sees_the_full_clip():
    seen = {}
    server = JarvisServer(
        FakeAssistant(),
        stt=RecordingSTT(seen),
        tts=FakeTTS(),
        speaker_verifier=RecordingVerifier(seen),
    )
    speech = np.ones(16000, dtype=np.int16) * 1000      # 1.0s speech
    silence = np.zeros(16000, dtype=np.int16)           # 1.0s silence
    pcm = np.concatenate([speech, silence]).tobytes()

    asyncio.run(server._handle_audio(FakeWS(), pcm))

    assert seen["stt_len"] == 32000
    assert 16000 <= seen["verify_len"] <= 16000 + 2800  # speech whole, tail trimmed
