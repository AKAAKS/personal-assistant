"""Clipboard tool handlers — read + write the PC clipboard."""

from jarvisd.tools import clipboard


def test_get_clipboard_returns_text(monkeypatch):
    monkeypatch.setattr(clipboard, "_paste", lambda: "hello world")
    out = clipboard.handlers()["get_clipboard"]({})
    assert "hello world" in out


def test_get_clipboard_reports_empty(monkeypatch):
    monkeypatch.setattr(clipboard, "_paste", lambda: "")
    out = clipboard.handlers()["get_clipboard"]({})
    assert "empty" in out.lower()


def test_set_clipboard_copies_text(monkeypatch):
    saved = {}
    monkeypatch.setattr(clipboard, "_copy", lambda t: saved.__setitem__("t", t))
    out = clipboard.handlers()["set_clipboard"]({"text": "copy me"})
    assert saved["t"] == "copy me"
    assert "copied" in out.lower()


def test_set_clipboard_requires_text():
    out = clipboard.handlers()["set_clipboard"]({})
    assert "error" in out.lower()
