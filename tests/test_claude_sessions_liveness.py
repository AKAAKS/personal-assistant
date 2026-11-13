"""Liveness: a record stops being reported when its Claude process is gone.

Not a TTL. A Claude Code session can sit on a permission prompt for hours, so
an activity-style timer would expire a state that is still true. The record
carries the pid of its `claude.exe` ancestor and that process's start time, and
the start time is what defeats pid reuse.

`walker` and `probe` are injected throughout, so none of these tests needs a
real process tree.
"""

from jarvisd import claude_sessions as cs


def _fake_walker(chain):
    """A process walk from a list of (pid, name), innermost first.

    The create time each entry reports is derived from its pid so the tests can
    assert on it without caring what it is.
    """
    def walk(_start_pid=None):
        for pid, name in chain:
            yield pid, name, float(pid) * 10.0
    return walk


def _probe(table):
    """A pid -> create_time lookup; a pid absent from the table is gone."""
    return lambda pid: table.get(pid)


def _rec(**kw):
    record = {"state": cs.WORKING, "since": 100.0, "updated_ts": 100.0,
              "claude_pid": None, "claude_started": None}
    record.update(kw)
    return record


# --- claude_ancestor -------------------------------------------------------

def test_finds_claude_in_the_ancestor_chain_not_just_the_parent():
    chain = [(10, "python.exe"), (11, "cmd.exe"), (12, "claude.exe"),
             (13, "powershell.exe")]
    assert cs.claude_ancestor(walker=_fake_walker(chain))[0] == 12


def test_it_reports_the_start_time_along_with_the_pid():
    chain = [(10, "python.exe"), (12, "claude.exe")]
    assert cs.claude_ancestor(walker=_fake_walker(chain)) == (12, 120.0)


def test_it_takes_the_nearest_claude_when_sessions_are_nested():
    chain = [(10, "python.exe"), (12, "claude.exe"), (20, "claude.exe")]
    assert cs.claude_ancestor(walker=_fake_walker(chain))[0] == 12


def test_the_process_name_match_ignores_case():
    chain = [(10, "python.exe"), (12, "Claude.EXE")]
    assert cs.claude_ancestor(walker=_fake_walker(chain))[0] == 12


def test_no_claude_ancestor_returns_none():
    chain = [(10, "python.exe"), (11, "cmd.exe"), (13, "powershell.exe")]
    assert cs.claude_ancestor(walker=_fake_walker(chain)) is None


def test_a_walker_that_raises_returns_none():
    def walk(_start_pid=None):
        raise RuntimeError("no process access")
    assert cs.claude_ancestor(walker=walk) is None


# --- is_live ---------------------------------------------------------------

def test_a_record_whose_pid_is_gone_is_not_live():
    record = _rec(claude_pid=4242, claude_started=120.0)
    assert cs.is_live(record, probe=_probe({}), now=lambda: 200.0) is False


def test_a_reused_pid_with_a_different_start_time_is_not_live():
    """Same pid, different create_time -> an unrelated process; the session is gone."""
    record = _rec(claude_pid=4242, claude_started=120.0)
    assert cs.is_live(record, probe=_probe({4242: 999.0}), now=lambda: 200.0) is False


def test_a_live_matching_pid_is_live_however_old_the_record():
    record = _rec(claude_pid=4242, claude_started=120.0, updated_ts=0.0)
    now = lambda: cs.NO_PID_TTL_SEC * 10
    assert cs.is_live(record, probe=_probe({4242: 120.0}), now=now) is True


def test_a_process_we_cannot_inspect_counts_as_live():
    """psutil.AccessDenied means it is running, just not readable by us.
    Reporting it dead would be a confident wrong answer."""
    record = _rec(claude_pid=4242, claude_started=120.0)
    probe = lambda pid: cs.START_UNKNOWN
    assert cs.is_live(record, probe=probe, now=lambda: 200.0) is True


def test_a_record_with_no_pid_expires_after_the_ttl():
    record = _rec(updated_ts=0.0)
    now = lambda: cs.NO_PID_TTL_SEC + 1
    assert cs.is_live(record, probe=_probe({}), now=now) is False


def test_a_record_with_no_pid_survives_inside_the_ttl():
    record = _rec(updated_ts=0.0)
    now = lambda: cs.NO_PID_TTL_SEC - 1
    assert cs.is_live(record, probe=_probe({}), now=now) is True


def test_a_record_with_no_timestamps_at_all_is_not_live():
    assert cs.is_live({"state": cs.WORKING}, probe=_probe({}), now=lambda: 5.0) is False


# --- apply_event records the ancestor --------------------------------------

def test_apply_event_stores_the_claude_pid_and_start_time(tmp_path):
    cs.apply_event({"hook_event_name": "SessionStart", "session_id": "s1"},
                   directory=tmp_path, ancestor=lambda: (99, 7.5))
    rec = cs.read_raw(directory=tmp_path)[0]
    assert rec["claude_pid"] == 99
    assert rec["claude_started"] == 7.5


def test_apply_event_records_no_pid_when_there_is_no_claude_ancestor(tmp_path):
    cs.apply_event({"hook_event_name": "SessionStart", "session_id": "s1"},
                   directory=tmp_path, ancestor=lambda: None)
    rec = cs.read_raw(directory=tmp_path)[0]
    assert rec["claude_pid"] is None
    assert rec["claude_started"] is None


def test_a_failing_ancestor_lookup_still_writes_the_record(tmp_path):
    def boom():
        raise RuntimeError("psutil exploded")
    cs.apply_event({"hook_event_name": "SessionStart", "session_id": "s1"},
                   directory=tmp_path, ancestor=boom)
    assert cs.read_raw(directory=tmp_path)[0]["state"] == cs.IDLE


# --- read_sessions ---------------------------------------------------------

def _write(directory, session_id, *, state, since, pid, started):
    cs.apply_event({"hook_event_name": "SessionStart", "session_id": session_id},
                   directory=directory, now=lambda: since,
                   ancestor=lambda: (pid, started) if pid else None)
    if state != cs.IDLE:
        event = {cs.WAITING: "PermissionRequest", cs.DONE: "Stop",
                 cs.WORKING: "UserPromptSubmit"}[state]
        cs.apply_event({"hook_event_name": event, "session_id": session_id},
                       directory=directory, now=lambda: since,
                       ancestor=lambda: (pid, started) if pid else None)


def test_read_sessions_drops_dead_records(tmp_path):
    _write(tmp_path, "alive", state=cs.WORKING, since=100.0, pid=11, started=1.0)
    _write(tmp_path, "dead", state=cs.WORKING, since=100.0, pid=22, started=2.0)
    live = cs.read_sessions(directory=tmp_path, now=lambda: 200.0,
                            probe=_probe({11: 1.0}))
    assert [r["session_id"] for r in live] == ["alive"]


def test_read_sessions_orders_by_most_recent_state_change(tmp_path):
    _write(tmp_path, "old", state=cs.WORKING, since=100.0, pid=11, started=1.0)
    _write(tmp_path, "new", state=cs.WAITING, since=300.0, pid=12, started=2.0)
    _write(tmp_path, "mid", state=cs.DONE, since=200.0, pid=13, started=3.0)
    live = cs.read_sessions(directory=tmp_path, now=lambda: 400.0,
                            probe=_probe({11: 1.0, 12: 2.0, 13: 3.0}))
    assert [r["session_id"] for r in live] == ["new", "mid", "old"]


def test_read_sessions_on_a_missing_directory_is_empty(tmp_path):
    assert cs.read_sessions(directory=tmp_path / "nope", now=lambda: 1.0,
                            probe=_probe({})) == []
