"""Turn model markdown into clean spoken prose for TTS + terminal display.

JARVIS replies are read aloud, so any markdown the model emits (bold, bullet
lists, headings, emoji) would otherwise be narrated verbatim. strip_markdown is
applied to the OUTGOING reply only -- never to conversation history.
"""

import re

# Broad emoji / pictograph / dingbat ranges + variation selector.
_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"  # symbols, pictographs, emoticons, supplemental
    "\U00002600-\U000027BF"  # misc symbols + dingbats
    "\U0001F1E6-\U0001F1FF"  # regional indicators
    "\U00002B00-\U00002BFF"  # misc symbols and arrows
    "️"                  # variation selector-16
    "]"
)


def strip_markdown(text: str) -> str:
    """Return `text` as plain spoken prose: markup removed, lists flattened."""
    if not text:
        return text

    lines = []
    for raw in text.splitlines():
        line = raw.strip()
        line = re.sub(r"^#{1,6}\s*", "", line)  # "## Title" -> "Title"
        m = re.match(r"^(?:[-*+]|\d+[.)])\s+(.*)$", line)  # list markers
        if m:
            line = m.group(1).strip()
            if line and line[-1] not in ".!?:;,":
                line += "."  # give the TTS a sentence boundary to pause on
        lines.append(line)
    text = "\n".join(lines)

    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)          # **bold**
    text = re.sub(r"__(.+?)__", r"\1", text)              # __bold__
    text = re.sub(r"\*(.+?)\*", r"\1", text)              # *italic*
    text = re.sub(r"(?<!\w)_(.+?)_(?!\w)", r"\1", text)   # _italic_
    text = re.sub(r"`(.+?)`", r"\1", text)                # `code`
    text = re.sub(r"\[(.+?)\]\((.+?)\)", r"\1", text)     # [text](url) -> text
    text = _EMOJI_RE.sub("", text)

    text = re.sub(r"\n{2,}", "\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()
