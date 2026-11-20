"""Edge TTS: bounded per-attempt budget, one retry, then None (caller falls back to SAPI).

Offline and fast: the worker is replaced on the instance, and the timeout path
uses a shrunken per-attempt budget plus an Event, never a real multi-second sleep.
"""

import threading

import voice.tts_edge as tts_edge
from voice.tts_edge import EDGE_ATTEMPT_TIMEOUT_SEC, EDGE_ATTEMPTS, EdgeTTS


class _Calls:
    def __init__(self):
        self.count = 0
        self.results = []


def _install(edge, behaviours):
    """Replace the worker; behaviours[i] is 'ok' or 'fail' for attempt i (last repeats)."""
    calls = _Calls()

    def worker(text, result):
        kind = behaviours[min(calls.count, len(behaviours) - 1)]
        calls.count += 1
        calls.results.append(result)
        if kind == "ok":
            result["wav"] = b"RIFFok"

    edge._synth_worker = worker
    return calls


def test_a_failed_first_attempt_is_retried_once():
    edge = EdgeTTS()
    calls = _install(edge, ["fail", "ok"])
    assert edge.synthesize("hello") == b"RIFFok"
    assert calls.count == 2


def test_two_failures_return_none_so_the_caller_falls_back():
    edge = EdgeTTS()
    calls = _install(edge, ["fail"])
    assert edge.synthesize("hello") is None
    assert calls.count == EDGE_ATTEMPTS


def test_a_success_is_not_retried():
    edge = EdgeTTS()
    calls = _install(edge, ["ok"])
    assert edge.synthesize("hello") is not None
    assert calls.count == 1


def test_each_attempt_gets_a_fresh_result_dict():
    edge = EdgeTTS()
    calls = _install(edge, ["fail", "ok"])
    edge.synthesize("hello")
    assert calls.results[0] is not calls.results[1]


def test_a_late_write_from_an_abandoned_attempt_does_not_leak(monkeypatch):
    """A timed-out worker is abandoned, not cancelled, and may still write
    result['wav'] later. It must not reach the caller through attempt 2."""
    monkeypatch.setattr(tts_edge, "EDGE_ATTEMPT_TIMEOUT_SEC", 0.05)
    edge = EdgeTTS()
    release = threading.Event()
    n = {"c": 0}
    stale = {}

    def worker(text, result):
        n["c"] += 1
        if n["c"] == 1:
            stale["thread"] = threading.current_thread()
            release.wait(5)
            result["wav"] = b"STALE"
        else:
            # Attempt 2 fails without writing. Before it finishes, let the
            # abandoned attempt-1 worker land its stale write, so the write is
            # in place before synthesize reads attempt 2's result.
            release.set()
            stale["thread"].join(5)

    edge._synth_worker = worker
    try:
        assert edge.synthesize("hello") is None
        assert not stale["thread"].is_alive()
    finally:
        release.set()


def test_a_hung_attempt_times_out_and_is_retried(monkeypatch):
    monkeypatch.setattr(tts_edge, "EDGE_ATTEMPT_TIMEOUT_SEC", 0.05)
    edge = EdgeTTS()
    release = threading.Event()
    n = {"c": 0}

    def worker(text, result):
        n["c"] += 1
        if n["c"] == 1:
            release.wait(5)
        else:
            result["wav"] = b"RIFFok"

    edge._synth_worker = worker
    try:
        assert edge.synthesize("hello") == b"RIFFok"
        assert n["c"] == 2
    finally:
        release.set()


def test_the_per_attempt_budget_is_five_seconds():
    from voice.tts import SYNTH_TIMEOUT_SEC

    assert EDGE_ATTEMPT_TIMEOUT_SEC == 5.0
    assert SYNTH_TIMEOUT_SEC != EDGE_ATTEMPT_TIMEOUT_SEC
