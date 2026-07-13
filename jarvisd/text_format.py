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

    # --- Emphasis stripping -------------------------------------------------
    # This text is about to be spoken by a TTS engine. A literal "*" or "_"
    # leaking into speech is the primary failure mode; stripping emphasis a
    # little too eagerly is a benign, secondary one. So when a case is
    # ambiguous, we STRIP -- we do not try to special-case every identifier
    # shape that might collide with markup syntax.
    #
    # An unbalanced run of 3+ asterisks (e.g. "*** waiting ***") would
    # otherwise make the lazy **bold** pairing below consume unevenly,
    # leaving single stray asterisks behind that are indistinguishable from
    # genuine multiplication. Collapse any such run to a clean pair first so
    # it pairs up correctly (or falls through to the leftover-run sweep).
    text = re.sub(r"\*{3,}", "**", text)

    # Paired bold markers are unwrapped unconditionally, even when glued to
    # surrounding text ("**cat**s" -> "cats") or when the inner text happens
    # to look like a bare identifier ("__init__" -> "init"). Yes, this mangles
    # the Python dunder -- that's an ACCEPTED outcome. This is a home voice
    # assistant (lights, music, reminders), not a code reader; do not re-add
    # a dunder/identifier special case here.
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)  # **bold**
    text = re.sub(r"__(.+?)__", r"\1", text)      # __bold__

    # Leftover single "*" is swept up next: a run of `*` counts as markup
    # (and is removed) if it touches a word character on either side. A
    # standalone `*` with whitespace on both sides survives, since that's
    # multiplication ("3 * 4 * 5"), not italics -- the one asterisk case
    # that's preserved.
    text = re.sub(r"(?<=\w)\*+|\*+(?=\w)", "", text)

    # A run of 2+ asterisks has no multiplication carve-out -- it is always
    # markup (e.g. an unbalanced "**" from a truncated bold pair) and must
    # be removed unconditionally, even when flanked by whitespace on both
    # sides. Only a genuinely isolated SINGLE "*" survives that sweep.
    text = re.sub(r" *\*{2,} *", " ", text)

    # Leftover single "_" is markup unless it is exactly one underscore
    # sitting between word characters on BOTH sides -- that's snake_case
    # ("snake_case_name"), the one underscore case that's preserved, and it
    # falls out of this rule structurally (not as a special case): a run of
    # underscores with a word char on only one side, or a run of 2+
    # underscores between word chars, is markup and gets removed.
    text = re.sub(
        r"(?<!\w)_+(?=\w)|(?<=\w)_+(?!\w)|(?<=\w)_{2,}(?=\w)", "", text
    )

    text = re.sub(r"`(.+?)`", r"\1", text)                # `code`
    text = re.sub(r"\[(.+?)\]\((.*?)\)", r"\1", text)     # [text](url) -> text
    text = _EMOJI_RE.sub("", text)

    text = re.sub(r"\n{2,}", "\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()
