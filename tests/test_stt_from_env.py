"""The shipped Whisper defaults must BE the fix, not just document it.

`distil-large-v3` + float16 and the vocabulary prompt are what stopped the live
log producing "Run Claude spelled C-L-A-U-D-E in a domino" and "service, run
clored in a terminal". Those values lived only in a developer's untracked .env
while jarvisd defaulted to `small`/`int8_float16`, so a fresh clone got the
mangling back. The wiring also existed twice (jarvisd/__main__ and
scripts/mic_check), so the two could disagree about what the default even was.
"""

import voice.stt as stt


class FakeModel:
    def __init__(self, model_size, device=None, compute_type=None):
        self.args = (model_size, device, compute_type)


def _capture(monkeypatch, **env):
    seen = {}

    class Recorder(stt.SpeechToText):
        def __init__(self, **kwargs):
            seen.update(kwargs)

    for k in ("WHISPER_MODEL", "WHISPER_DEVICE", "WHISPER_COMPUTE_TYPE"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(stt, "SpeechToText", Recorder)
    Recorder.from_env()
    return seen


def test_defaults_are_the_measured_fix(monkeypatch):
    seen = _capture(monkeypatch)
    assert seen["model_size"] == "distil-large-v3"
    assert seen["compute_type"] == "float16"
    assert seen["device"] == "cuda"


def test_env_still_overrides_every_default(monkeypatch):
    seen = _capture(monkeypatch, WHISPER_MODEL="small",
                    WHISPER_DEVICE="cpu", WHISPER_COMPUTE_TYPE="int8")
    assert seen["model_size"] == "small"
    assert seen["device"] == "cpu"
    assert seen["compute_type"] == "int8"


def test_a_cpu_only_machine_is_not_stranded(monkeypatch):
    """float16 on a CPU raises inside faster-whisper; __init__ must fall back
    rather than taking the daemon down. This is what makes the GPU-shaped
    default safe to ship."""
    attempts = []

    def boom(model_size, device=None, compute_type=None):
        attempts.append((device, compute_type))
        if device != "cpu":
            raise RuntimeError("CUDA driver not found")
        return FakeModel(model_size, device, compute_type)

    monkeypatch.setattr(stt, "_load_nvidia_dlls", lambda: None)
    import sys
    import types
    fake = types.ModuleType("faster_whisper")
    fake.WhisperModel = boom
    monkeypatch.setitem(sys.modules, "faster_whisper", fake)

    stt.SpeechToText(model_size="distil-large-v3", device="cuda", compute_type="float16")

    assert attempts[0] == ("cuda", "float16")
    assert attempts[-1][0] == "cpu", "no CPU fallback was attempted"
