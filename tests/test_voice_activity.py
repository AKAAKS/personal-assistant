"""The channel the voice client uses to tell the tray what it is doing.

The tray and the voice client are separate processes (the tray supervises and
relaunches the client), so "I heard you and I'm thinking" has to cross a process
boundary. A single small file is the whole mechanism: the client writes a state
on every transition, the tray polls it.

Two things make it safe rather than merely simple. Writes are atomic, so the
tray can never read a half-written file and show a wrong state. And every record
carries a timestamp, so a client that dies mid-"thinking" expires instead of
freezing the icon on a lie -- a stale read must be indistinguishable from no
read at all.
"""

import json
import os
import time

from clients import activity


def test_round_trips_a_state(tmp_path):
    p = str(tmp_path / "act.json")
    activity.write(activity.THINKING, path=p)
    rec = activity.read(path=p)
    assert rec["state"] == activity.THINKING


def test_round_trips_a_reason(tmp_path):
    """The reason distinguishes "couldn't make out words" from "not your voice"."""
    p = str(tmp_path / "act.json")
    activity.write(activity.MISSED, reason=activity.NO_WORDS, path=p)
    assert activity.read(path=p)["reason"] == activity.NO_WORDS


def test_reason_defaults_to_none(tmp_path):
    p = str(tmp_path / "act.json")
    activity.write(activity.SPEAKING, path=p)
    assert activity.read(path=p)["reason"] is None


def test_records_the_write_time(tmp_path):
    p = str(tmp_path / "act.json")
    activity.write(activity.HEARING, path=p, now=lambda: 1000.0)
    # Read on the same frozen clock: on the real one this record is 1970-era
    # and correctly expires, which is what test_stale_record_reads_as_none pins.
    assert activity.read(path=p, now=lambda: 1000.0)["ts"] == 1000.0


def test_latest_write_wins(tmp_path):
    p = str(tmp_path / "act.json")
    activity.write(activity.HEARING, path=p)
    activity.write(activity.THINKING, path=p)
    assert activity.read(path=p)["state"] == activity.THINKING


# --- the failure modes that would otherwise show a wrong state --------------


def test_missing_file_reads_as_none(tmp_path):
    """First run, before the client has said anything."""
    assert activity.read(path=str(tmp_path / "nope.json")) is None


def test_stale_record_reads_as_none(tmp_path):
    """A client that died mid-"thinking" must not freeze the icon on it."""
    p = str(tmp_path / "act.json")
    activity.write(activity.THINKING, path=p, now=lambda: 1000.0)
    assert activity.read(path=p, now=lambda: 1000.0 + activity.TTL_SEC + 1) is None


def test_fresh_record_survives_just_inside_the_ttl(tmp_path):
    p = str(tmp_path / "act.json")
    activity.write(activity.THINKING, path=p, now=lambda: 1000.0)
    rec = activity.read(path=p, now=lambda: 1000.0 + activity.TTL_SEC - 1)
    assert rec["state"] == activity.THINKING


def test_corrupt_file_reads_as_none(tmp_path):
    """Never raise into the tray's render loop over a damaged file."""
    p = str(tmp_path / "act.json")
    with open(p, "w", encoding="utf-8") as f:
        f.write("{not json at all")
    assert activity.read(path=p) is None


def test_record_without_a_state_reads_as_none(tmp_path):
    p = str(tmp_path / "act.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"ts": time.time()}, f)
    assert activity.read(path=p) is None


def test_write_to_an_unwritable_path_never_raises(tmp_path):
    """A failed status write must not take the voice client down with it: the
    indicator is a nicety, the assistant is the product."""
    activity.write(activity.THINKING, path=str(tmp_path / "no_such_dir" / "act.json"))


def test_write_creates_the_parent_directory(tmp_path):
    p = str(tmp_path / "fresh" / "act.json")
    activity.write(activity.ARMED, path=p, make_parents=True)
    assert activity.read(path=p)["state"] == activity.ARMED


def test_write_is_atomic_leaving_no_partial_file(tmp_path):
    """The tray polls this constantly; it must never catch a half-written file."""
    p = str(tmp_path / "act.json")
    activity.write(activity.SPEAKING, path=p)
    leftovers = [n for n in os.listdir(tmp_path) if n != "act.json"]
    assert leftovers == []
