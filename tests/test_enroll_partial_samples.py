"""Ctrl-C during enrollment must keep the samples already recorded.

`embeddings = _collect_voice(...)` only binds on a NORMAL return, so a
KeyboardInterrupt on prompt 5 of 6 left the caller's list empty and the script
printed "Only captured 0 sample(s)" -- discarding four good recordings and
making the user start over. It also made MIN_SAMPLES (4) unreachable: the
function returned exactly N_SAMPLES (6) or raised, so the "you already have
enough, stop early" affordance could never trigger.

Fixed by having _collect_voice append to a list the caller owns.
"""

import builtins

import numpy as np
import pytest

from clients import enroll_voice


class FakeMic:
    def clear(self):
        pass


def _patch(monkeypatch, *, interrupt_after):
    """Record `interrupt_after` good samples, then Ctrl-C."""
    seen = {"n": 0}

    def fake_input(_prompt):
        if seen["n"] >= interrupt_after:
            raise KeyboardInterrupt
        return ""

    def fake_record(mic, vad):
        seen["n"] += 1
        return np.ones(enroll_voice.MIN_SAMPLES_LEN, dtype=np.int16)

    monkeypatch.setattr(builtins, "input", fake_input)
    monkeypatch.setattr(enroll_voice, "_record", fake_record)
    monkeypatch.setattr(enroll_voice, "embed_audio",
                        lambda enc, audio, backend: np.ones(192, dtype=np.float32))


def test_samples_recorded_before_ctrl_c_are_kept(monkeypatch, capsys):
    _patch(monkeypatch, interrupt_after=5)
    collected = []
    with pytest.raises(KeyboardInterrupt):
        enroll_voice._collect_voice(None, "ecapa", FakeMic(), None, collected)
    assert len(collected) == 5, (
        "a Ctrl-C threw away every sample recorded so far"
    )


def test_a_full_run_still_collects_all_samples(monkeypatch, capsys):
    _patch(monkeypatch, interrupt_after=enroll_voice.N_SAMPLES + 1)
    collected = []
    enroll_voice._collect_voice(None, "ecapa", FakeMic(), None, collected)
    assert len(collected) == enroll_voice.N_SAMPLES


def test_min_samples_is_reachable(monkeypatch, capsys):
    """Stopping after MIN_SAMPLES must leave a usable enrollment."""
    _patch(monkeypatch, interrupt_after=enroll_voice.MIN_SAMPLES)
    collected = []
    with pytest.raises(KeyboardInterrupt):
        enroll_voice._collect_voice(None, "ecapa", FakeMic(), None, collected)
    assert len(collected) >= enroll_voice.MIN_SAMPLES
