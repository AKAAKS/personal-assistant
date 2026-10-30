"""_StreamToLogger: captures print() into the logging system under pythonw,
so it lands in the rotating log instead of crashing or holding a second handle
on jarvisd.log (which would block rotation on Windows)."""

import logging

from jarvisd.__main__ import _StreamToLogger


class _Collector(logging.Handler):
    def __init__(self):
        super().__init__()
        self.msgs = []

    def emit(self, record):
        self.msgs.append(record.getMessage())


def _wire():
    logger = logging.getLogger("test.streamlogger")
    logger.handlers = []
    logger.propagate = False
    logger.setLevel(logging.INFO)
    h = _Collector()
    logger.addHandler(h)
    return logger, h


def test_buffers_until_newline():
    logger, h = _wire()
    s = _StreamToLogger(logger, logging.INFO)
    s.write("Spotify")      # no newline -> nothing emitted yet
    assert h.msgs == []
    s.write(" connected!\n")
    assert h.msgs == ["Spotify connected!"]


def test_multiple_lines_in_one_write():
    logger, h = _wire()
    s = _StreamToLogger(logger, logging.INFO)
    s.write("line one\nline two\n")
    assert h.msgs == ["line one", "line two"]


def test_blank_lines_are_ignored():
    logger, h = _wire()
    s = _StreamToLogger(logger, logging.INFO)
    s.write("\n   \n")
    assert h.msgs == []


def test_flush_emits_unterminated_remainder():
    logger, h = _wire()
    s = _StreamToLogger(logger, logging.INFO)
    s.write("no newline here")
    assert h.msgs == []
    s.flush()
    assert h.msgs == ["no newline here"]
