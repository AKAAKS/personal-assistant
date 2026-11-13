"""The announcement tick: told about an unattended session without asking.

The trigger is `idle_ts` -- Claude Code's own "idle ~60s" notification -- which
is why no debounce is needed here: if the user were at the keyboard they would
have answered the prompt and it would never have fired. Announcing while they
are sitting in front of the screen is the design's third anti-goal.

`announcement_for` is static and pure so every wording and guard case is
checked without a server, a clock or a notifier.
"""

import asyncio

from jarvisd import claude_sessions as cs
from jarvisd import server as server_module
from jarvisd.server import JarvisServer


class FakeAssistant:
    def process(self, text):
        return f"echo: {text}"

    def reset_conversation(self):
        pass


class RecordingNotifier:
    def __init__(self):
        self.calls = []

    def notify(self, text, priority="low", *, perishable=False, local_only=False):
        self.calls.append((text, priority, perishable, local_only))

    def drain(self):
        return []


def _rec(state, *, idle=True, session_id="s1", project="proj", since=100.0,
         message=None, idle_ts=None):
    return {
        "session_id": session_id,
        "state": state,
        "project": project,
        "since": since,
        "updated_ts": since,
        "idle_ts": (idle_ts if idle_ts is not None else (since + 60.0 if idle else None)),
        "last_message": message,
        "tool_name": None,
    }


def _server(records, notifier):
    server = JarvisServer(FakeAssistant(), notifier=notifier, port=0)
    server._read_claude_sessions = lambda: list(records)
    return server


# --- wording ---------------------------------------------------------------

def test_a_waiting_session_announces_that_it_needs_you():
    text = JarvisServer.announcement_for(_rec(cs.WAITING))
    assert text == "Claude needs your input in proj."


def test_a_finished_session_announces_what_it_said():
    text = JarvisServer.announcement_for(_rec(cs.DONE, message="pushed the commit"))
    assert "Claude finished in proj" in text
    assert "pushed the commit" in text


def test_a_finished_session_with_no_message_still_announces():
    assert JarvisServer.announcement_for(_rec(cs.DONE)) == "Claude finished in proj."


def test_a_markdown_message_is_flattened_for_speech():
    text = JarvisServer.announcement_for(_rec(cs.DONE, message="**done**\n\n- a\n- b"))
    assert "\n" not in text and "**" not in text


def test_a_session_with_no_project_still_announces():
    text = JarvisServer.announcement_for(_rec(cs.WAITING, project=None))
    assert "Claude needs your input" in text


def test_a_working_session_announces_nothing():
    assert JarvisServer.announcement_for(_rec(cs.WORKING, idle=True)) is None


def test_a_wait_younger_than_the_threshold_announces_nothing():
    """Being at the keyboard must stay silent. For a permission prompt the
    debounce is the length of the wait itself: answer it and nothing is said."""
    record = _rec(cs.WAITING, idle=False, since=1000.0)
    now = lambda: 1000.0 + server_module.WAIT_ANNOUNCE_SEC - 1
    assert JarvisServer.announcement_for(record, now=now) is None


def test_an_open_permission_prompt_announces_without_an_idle_signal():
    """Measured live: Claude Code sends NO idle notification while a
    permission dialog is open -- a prompt sat `waiting` for 197s with
    idle_ts still null -- so gating this case on idle meant it could never
    fire at all. The wait itself is the evidence nobody is answering."""
    record = _rec(cs.WAITING, idle=False, since=1000.0)
    now = lambda: 1000.0 + server_module.WAIT_ANNOUNCE_SEC
    assert JarvisServer.announcement_for(record, now=now) == (
        "Claude needs your input in proj.")


def test_an_idle_signal_still_announces_a_wait_immediately():
    """If Claude Code ever does send idle during a dialog, honour it rather
    than waiting out the threshold."""
    record = _rec(cs.WAITING, idle=True, since=1000.0)
    now = lambda: 1000.0 + 1
    assert JarvisServer.announcement_for(record, now=now) is not None


def test_a_finished_session_still_needs_the_idle_signal():
    """The `done` case keeps the original debounce, because it demonstrably
    works: idle fires for a session sitting at an empty prompt."""
    record = _rec(cs.DONE, idle=False, since=1000.0, message="pushed it")
    now = lambda: 1000.0 + 99999
    assert JarvisServer.announcement_for(record, now=now) is None


def test_a_long_running_turn_still_announces_nothing():
    """Only a WAIT earns the elapsed-time route; a long turn is not news."""
    record = _rec(cs.WORKING, idle=False, since=1000.0)
    now = lambda: 1000.0 + 99999
    assert JarvisServer.announcement_for(record, now=now) is None


def test_the_spoken_message_is_trimmed_to_about_a_sentence():
    """Production read out ~500 characters of a merge report as a toast."""
    long_message = ("Merged, pushed, and the daemon is back up on the new code. "
                    "Master is 5722a68, a no-ff merge of all twelve commits, and "
                    "the suite is green apart from three known failures.")
    text = JarvisServer.announcement_for(
        _rec(cs.DONE, message=long_message), now=lambda: 1000.0)
    assert "Merged, pushed, and the daemon is back up on the new code." in text
    assert "5722a68" not in text
    assert len(text) < 200


def test_a_message_with_no_sentence_end_is_cut_cleanly():
    text = JarvisServer.announcement_for(
        _rec(cs.DONE, message="x" * 400), now=lambda: 1000.0)
    assert len(text) < 200


def test_a_short_message_is_quoted_whole():
    text = JarvisServer.announcement_for(
        _rec(cs.DONE, message="pushed the commit"), now=lambda: 1000.0)
    assert text.endswith("pushed the commit")


def test_a_stale_idle_from_a_previous_state_announces_nothing():
    """idle_ts < since: the state moved on after the idle fired, so the idle
    describes a state that is gone."""
    record = _rec(cs.DONE, since=500.0, idle_ts=400.0, message="old news")
    assert JarvisServer.announcement_for(record) is None


def test_an_idle_at_the_same_moment_as_the_state_change_announces():
    record = _rec(cs.WAITING, since=500.0, idle_ts=500.0)
    assert JarvisServer.announcement_for(record) is not None


def test_an_idle_session_announces_nothing():
    """A terminal sitting at an empty prompt is not news, same as a long turn."""
    assert JarvisServer.announcement_for(_rec(cs.IDLE, idle=True)) is None


def test_an_unknown_state_announces_nothing():
    assert JarvisServer.announcement_for(_rec("teleporting")) is None


# --- the tick --------------------------------------------------------------

def _tick(server):
    asyncio.run(server._announce_idle_sessions())


def test_a_tick_announces_a_waiting_session():
    notifier = RecordingNotifier()
    _tick(_server([_rec(cs.WAITING)], notifier))
    assert notifier.calls[0][0] == "Claude needs your input in proj."


def test_the_announcement_is_perishable():
    notifier = RecordingNotifier()
    _tick(_server([_rec(cs.WAITING)], notifier))
    text, priority, perishable, local_only = notifier.calls[0]
    assert perishable is True


def test_the_announcement_is_not_silenced_by_quiet_hours():
    """A perishable "high" notice inside the default 23:00-08:00 window is
    dropped, which would make the feature dark for nine hours a day -- and
    this user routinely works past 02:00 (see test_timer_urgency). The idle
    signal already proves they are at the PC but not at the keyboard, so the
    notice earns the same exemption a timer gets."""
    notifier = RecordingNotifier()
    _tick(_server([_rec(cs.WAITING)], notifier))
    assert notifier.calls[0][1] == "urgent"


def test_the_announcement_never_escalates_to_the_phone():
    """It ignores quiet hours so it still reaches someone working at 2am, but
    the notifier's fallback chain ends at a phone push -- and "Claude finished
    in X" buzzing a phone at 4am is exactly the notice nobody wants."""
    notifier = RecordingNotifier()
    _tick(_server([_rec(cs.WAITING)], notifier))
    assert notifier.calls[0][3] is True


def test_notify_is_never_called_on_the_event_loop_thread():
    """push_to_clients is _push_from_thread, which blocks the calling thread
    on a coroutine scheduled on the loop. Called FROM the loop thread that
    cannot progress, so it blocks until its 10s timeout and reaches nobody --
    exactly the spoken announcement this feature exists to deliver."""
    import threading

    class ThreadRecordingNotifier(RecordingNotifier):
        def __init__(self):
            super().__init__()
            self.threads = []

        def notify(self, text, priority="low", *, perishable=False,
                   local_only=False):
            self.threads.append(threading.get_ident())
            super().notify(text, priority, perishable=perishable,
                           local_only=local_only)

    notifier = ThreadRecordingNotifier()
    server = _server([_rec(cs.WAITING)], notifier)

    async def _run():
        server._loop = asyncio.get_running_loop()
        await server._announce_idle_sessions()
        return threading.get_ident()

    loop_thread = asyncio.run(_run())
    assert notifier.threads
    assert loop_thread not in notifier.threads


def test_a_pass_with_the_real_notifier_bridge_does_not_block_the_loop(tmp_path):
    """The end-to-end shape: a real Notifier wired to the real
    _push_from_thread bridge must complete promptly and actually reach
    push_proactive, rather than timing out against a stalled loop."""
    import time

    from jarvisd import db
    from jarvisd.notifier import Notifier

    conn = db.connect(str(tmp_path / "jarvis.db"))
    notifier = Notifier(conn, quiet_hours=None)
    server = JarvisServer(FakeAssistant(), notifier=notifier, port=0)
    server._read_claude_sessions = lambda: [_rec(cs.WAITING)]
    pushed = []

    async def _run():
        server._loop = asyncio.get_running_loop()
        notifier.push_to_clients = server._push_from_thread
        server.push_proactive = lambda text: _record(text)
        started = time.monotonic()
        await server._announce_idle_sessions()
        return time.monotonic() - started

    async def _record(text):
        pushed.append(text)
        return 1

    elapsed = asyncio.run(_run())
    assert pushed == ["Claude needs your input in proj."]
    assert elapsed < 2.0, f"the pass took {elapsed:.1f}s: the loop was blocked"


def test_each_transition_announces_once():
    notifier = RecordingNotifier()
    server = _server([_rec(cs.WAITING)], notifier)
    _tick(server)
    _tick(server)
    assert len(notifier.calls) == 1


def test_a_new_state_announces_again():
    notifier = RecordingNotifier()
    records = [_rec(cs.WAITING, since=100.0)]
    server = _server(records, notifier)
    _tick(server)
    records[:] = [_rec(cs.WORKING, since=200.0)]
    _tick(server)
    records[:] = [_rec(cs.WAITING, since=300.0)]
    _tick(server)
    assert len(notifier.calls) == 2


def test_two_sessions_each_announce():
    notifier = RecordingNotifier()
    records = [_rec(cs.WAITING, session_id="s1"),
               _rec(cs.DONE, session_id="s2", message="done here")]
    _tick(_server(records, notifier))
    assert len(notifier.calls) == 2


def test_a_working_session_is_never_announced():
    notifier = RecordingNotifier()
    _tick(_server([_rec(cs.WORKING)], notifier))
    assert notifier.calls == []


def test_a_notifier_failure_does_not_propagate():
    class Exploding(RecordingNotifier):
        def notify(self, *a, **kw):
            raise RuntimeError("notifier is down")

    _tick(_server([_rec(cs.WAITING)], Exploding()))


def test_with_no_notifier_the_tick_is_a_no_op():
    server = JarvisServer(FakeAssistant(), port=0)
    server._read_claude_sessions = lambda: [_rec(cs.WAITING)]
    asyncio.run(server._announce_idle_sessions())


def test_a_tick_failure_does_not_kill_the_loop(monkeypatch):
    """One bad pass must not take the watcher down for the rest of the day."""
    from jarvisd import server as server_module
    monkeypatch.setattr(server_module, "CLAUDE_WATCH_TICK_SEC", 0.001)
    notifier = RecordingNotifier()
    server = JarvisServer(FakeAssistant(), notifier=notifier, port=0)
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("state directory vanished")
        return [_rec(cs.WAITING)]

    server._read_claude_sessions = flaky

    async def _run():
        task = asyncio.create_task(server._claude_watch_tick())
        for _ in range(200):
            await asyncio.sleep(0.005)
            if notifier.calls:
                break
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(_run())
    assert calls["n"] >= 2
    assert notifier.calls


def test_the_dedupe_set_does_not_grow_without_bound():
    notifier = RecordingNotifier()
    records = [_rec(cs.WAITING, since=100.0)]
    server = _server(records, notifier)
    _tick(server)
    records[:] = [_rec(cs.WAITING, since=200.0)]
    _tick(server)
    assert len(server._claude_announced) == 1


def test_serve_forever_starts_and_cancels_the_watch_task():
    notifier = RecordingNotifier()
    server = JarvisServer(FakeAssistant(), notifier=notifier, port=0)

    async def _run():
        task = asyncio.create_task(server.serve_forever())
        await asyncio.sleep(0.05)
        started = server._claude_watch_task is not None
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return started, server._claude_watch_task.cancelled()

    started, cancelled = asyncio.run(_run())
    assert started is True
    assert cancelled is True


def test_serve_forever_does_not_start_the_watch_task_without_a_notifier():
    server = JarvisServer(FakeAssistant(), port=0)

    async def _run():
        task = asyncio.create_task(server.serve_forever())
        await asyncio.sleep(0.05)
        started = server._claude_watch_task is not None
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return started

    assert asyncio.run(_run()) is False


def test_the_tick_interval_is_fifteen_seconds():
    from jarvisd import server as server_module
    assert server_module.CLAUDE_WATCH_TICK_SEC == 15
