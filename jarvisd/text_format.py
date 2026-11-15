"""Turn model markdown into clean spoken prose for TTS + terminal display.

JARVIS replies are read aloud, so any markdown the model emits (bold, bullet
lists, headings, emoji) would otherwise be narrated verbatim. strip_markdown is
applied to the OUTGOING reply only -- never to conversation history.

Contract (wave 5, final): strip when ambiguous. There is exactly ONE
carve-out -- a single "_" flanked by a word character on both sides
(snake_case, e.g. "snake_case_name"). Every other "_" and every bare "*"
(including multiplication, e.g. "3 * 4") is deleted, deliberately. This is a
home voice assistant, not a code reader or calculator transcript; a literal
"*"/"_" leaking into speech is the failure mode that matters, and no
sequential, boundary-sensitive regex chain can be trusted to catch every new
adversarial shape of glued/nested/unbalanced markup (five review waves each
closed one leak and exposed the next). The function is made total instead:
an unconditional final sweep deletes every remaining "*" and every remaining
"_" except the one sanctioned snake_case shape, so this class of bug cannot
recur.
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


def _keep_snake_case_underscore(m: "re.Match") -> str:
    """re.sub callback for a maximal run of "_" characters.

    Delete the run unless it is exactly one "_" flanked by a word character
    on both sides (snake_case, e.g. "snake_case_name") -- the ONE sanctioned
    carve-out. Because the run matched by `_+` is always maximal, the
    character immediately outside either end can never itself be "_", so a
    plain `\\w` flank check can't be confused by an adjacent run.
    """
    run = m.group(0)
    if len(run) != 1:
        return ""  # a run of 2+ "_" is never snake_case
    s, start, end = m.string, m.start(), m.end()
    before = s[start - 1] if start > 0 else ""
    after = s[end] if end < len(s) else ""
    if re.match(r"\w", before) and re.match(r"\w", after):
        return run
    return ""


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

    text = re.sub(r"`(.+?)`", r"\1", text)                # `code`
    text = re.sub(r"\[(.+?)\]\((.*?)\)", r"\1", text)     # [text](url) -> text
    text = _EMOJI_RE.sub("", text)

    # --- Final safety sweep --------------------------------------------------
    # Wave 5 (final): no boundary-sensitive, sequential regex chain of
    # paired-emphasis substitutions can be trusted to catch every new
    # adversarial shape of glued/nested/unbalanced markup -- five review
    # waves each closed one leak and exposed the next. So this is the LAST
    # content-mutating step (right before whitespace normalization), and it
    # is unconditional by construction: it cannot miss a case because it
    # isn't conditional on any case.
    #
    # 1. Delete every remaining "_" EXCEPT a lone one flanked by a word
    #    character on both sides (snake_case, e.g. "snake_case_name") -- the
    #    ONE sanctioned carve-out. This must run BEFORE asterisks are
    #    deleted, while "*" is still literally present: `\w` never matches
    #    "*", so a "_" delimiter glued against a "*" run (e.g.
    #    "**bold**_italic_") is correctly seen as flanked by a non-word
    #    character (markup, not snake_case) and gets removed. Deleting "*"
    #    first would erase that boundary signal and make the leftover "_"
    #    indistinguishable from real snake_case -- that is the exact bug
    #    wave 4 fixed; do not reorder this back.
    # 2. Delete every remaining "*" character. No conditions, no
    #    lookarounds -- including multiplication ("3 * 4 * 5"); that
    #    carve-out is deliberately dropped (see module docstring).
    text = re.sub(r"_+", _keep_snake_case_underscore, text)
    text = text.replace("*", "")

    # Whitespace normalization. A content-mutating step above (most often
    # emoji removal, e.g. "\n\n\U0001F4A1 You have..." -> "\n\n You have...")
    # can leave a residual leading/trailing space on a line where the removed
    # token used to sit. Strip each line's edge whitespace before collapsing
    # blank lines/runs so no such residue survives into spoken prose.
    text = "\n".join(line.strip(" \t") for line in text.split("\n"))
    text = re.sub(r"\n{2,}", "\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def speak_duration(total_sec: int) -> str:
    """Render a duration as natural spoken English, e.g. "4 minutes 30 seconds".

    At most two units, largest first. A zero unit is omitted and a unit of 1 is
    singular. Once hours are present the seconds are dropped, since they are
    noise in speech. Zero (or less) renders as "no time".
    """
    if total_sec <= 0:
        return "no time"
    hours, rest = divmod(total_sec, 3600)
    minutes, seconds = divmod(rest, 60)
    if hours:
        units = [(hours, "hour"), (minutes, "minute")]
    else:
        units = [(minutes, "minute"), (seconds, "second")]
    return " ".join(
        f"{n} {unit}" if n == 1 else f"{n} {unit}s"
        for n, unit in units
        if n
    )


_WEEKDAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def _ordinal(n: int) -> str:
    # 11, 12 and 13 take "th" despite ending in 1, 2 and 3.
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def speak_recurrence(recurrence: str) -> str:
    """Render a stored recurrence for speech, so a TTS never reads machine
    syntax aloud: 'dow:0,2,4' -> 'on Mondays, Wednesdays and Fridays',
    'monthly:31' -> 'monthly on the 31st'. Plain words ('daily', 'monthly', ...)
    and anything unrecognised are returned unchanged.
    """
    from jarvisd.reminder_store import parse_recurrence  # local: avoid a hard import cycle

    try:
        kind, arg = parse_recurrence(recurrence)
    except ValueError:
        return recurrence
    if kind == "dow":
        names = [f"{_WEEKDAY_NAMES[d]}s" for d in arg]
        joined = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
        return f"on {joined}"
    if kind == "monthly" and arg is not None:
        return f"monthly on the {_ordinal(arg)}"
    return recurrence
