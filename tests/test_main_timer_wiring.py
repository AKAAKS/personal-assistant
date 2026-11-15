"""jarvisd's entry point must hand the timer store to the server.

JarvisServer.serve_forever starts the ring loop only when it has BOTH a timer_store
and a notifier. Forget either at the call site and every timer silently never
rings -- the original "it didn't alert me" bug, one layer up. These tests run the
real `run()` with a real server and a real timer store (port 0, nothing live).
"""

import argparse
import asyncio
import logging
from types import SimpleNamespace

import pytest

from jarvisd import __main__ as entry
from jarvisd import db
from jarvisd.server import JarvisServer
from jarvisd.timer_store import TimerStore

pytestmark = pytest.mark.anyio


class FakeAssistant:
    conversation: list = []

    class cost_tracker:
        @staticmethod
        def summary():
            return "budget"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def open_conns():
    """Connections the test opened; closed afterwards so tmp_path cleans up on Windows."""
    conns = []
    yield conns
    for c in conns:
        c.close()


def _run_entry_point(monkeypatch, tmp_path, open_conns, *, with_conn=True):
    """Run entry.run() up to serve_forever; return (server, timer_store, drive)."""
    conn = db.connect(str(tmp_path / "jarvis.db")) if with_conn else None
    if conn is not None:
        open_conns.append(conn)
    timer_store = TimerStore(conn) if with_conn else None
    created = {}

    class PortZeroServer(JarvisServer):
        def __init__(self, *args, **kwargs):
            kwargs["port"] = 0  # the live daemon owns 8765
            super().__init__(*args, **kwargs)
            created["server"] = self

    monkeypatch.setattr(entry, "JarvisServer", PortZeroServer)
    monkeypatch.setattr(entry, "create_assistant", lambda: (
        FakeAssistant(), [], conn, None, None, SimpleNamespace(), None, None, None, timer_store))
    monkeypatch.delenv("HA_TOKEN", raising=False)
    captured = {}
    monkeypatch.setattr(entry.asyncio, "run", lambda coro: captured.setdefault("coro", coro))

    entry.run(argparse.Namespace(no_speech=True, verbose=False), logging.getLogger("test"))
    return created["server"], timer_store, captured["coro"]


async def _serve_briefly(server, coro):
    """Run serve_forever just long enough to observe which tasks it started."""
    task = asyncio.create_task(coro)
    for _ in range(100):
        if server._ws_server is not None:
            break
        await asyncio.sleep(0.01)
    timer_task = server._timer_task
    running = timer_task is not None and not timer_task.done()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    return running


async def test_the_entry_point_passes_the_real_timer_store_to_the_server(
        monkeypatch, tmp_path, open_conns):
    server, timer_store, coro = _run_entry_point(monkeypatch, tmp_path, open_conns)
    coro.close()
    assert server.timer_store is timer_store
    assert server.notifier is not None


async def test_the_ring_loop_is_actually_running_once_the_entry_point_has_wired_the_server(
        monkeypatch, tmp_path, open_conns):
    server, _, coro = _run_entry_point(monkeypatch, tmp_path, open_conns)
    assert await _serve_briefly(server, coro) is True
    assert server._timer_task.cancelled()  # and it is torn down with the server


async def test_without_a_database_there_is_no_ring_loop_and_no_crash(
        monkeypatch, tmp_path, open_conns):
    server, _, coro = _run_entry_point(monkeypatch, tmp_path, open_conns, with_conn=False)
    assert server.timer_store is None
    assert await _serve_briefly(server, coro) is False
