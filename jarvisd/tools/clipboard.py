"""Clipboard tools — read and write the PC clipboard.

pyperclip is imported lazily inside the helpers so importing this module never
hard-fails if the package is missing; a failure degrades to a clear message.
"""

TOOLS = [
    {
        "name": "get_clipboard",
        "description": (
            "Read the current text on the PC clipboard. Use this when the user refers to "
            "something they just copied (e.g. 'summarize what I copied', 'translate this')."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "set_clipboard",
        "description": (
            "Put text on the PC clipboard so the user can paste it anywhere (Ctrl+V). "
            "Use this to hand the user a draft, a command, a link, etc."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "The text to copy to the clipboard."},
            },
            "required": ["text"],
        },
    },
]


def _paste() -> str:
    import pyperclip
    return pyperclip.paste()


def _copy(text: str) -> None:
    import pyperclip
    pyperclip.copy(text)


def handlers():
    """Clipboard tool handlers (no client needed)."""

    def get_clipboard(ti):
        try:
            text = _paste()
        except Exception as e:
            return f"Error reading clipboard: {e}"
        if not text:
            return "The clipboard is empty."
        return f"Clipboard contents:\n{text}"

    def set_clipboard(ti):
        text = ti.get("text")
        if not isinstance(text, str) or text == "":
            return "Error: text is required and must be a non-empty string."
        try:
            _copy(text)
        except Exception as e:
            return f"Error writing clipboard: {e}"
        return "Copied to your clipboard."

    return {
        "get_clipboard": get_clipboard,
        "set_clipboard": set_clipboard,
    }
