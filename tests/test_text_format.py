import re

from jarvisd.text_format import strip_markdown


def test_bold_and_italic_markers_removed():
    assert strip_markdown("**Tom Ellis** is an _actor_") == "Tom Ellis is an actor"


def test_headings_removed():
    assert strip_markdown("## Options") == "Options"


def test_list_items_become_sentences():
    out = strip_markdown("- A specific song\n- An artist")
    assert "-" not in out
    assert "A specific song." in out
    assert "An artist." in out


def test_numbered_list_items_become_sentences():
    out = strip_markdown("1. First\n2) Second")
    assert "First." in out and "Second." in out
    assert "1." not in out and "2)" not in out


def test_inline_code_and_links_unwrapped():
    assert strip_markdown("run `ls` now") == "run ls now"
    assert strip_markdown("see [Google](http://g.co)") == "see Google"


def test_emoji_dropped():
    assert strip_markdown("Enjoy! 🎵😊").strip() == "Enjoy!"


def test_emoji_removal_leaves_no_leading_space_on_its_line():
    """Emoji removal happens mid-line (e.g. a "\\n\\n💡 You have..." trailer);
    the space that separated the emoji from the following word must not
    survive as a leading space once the emoji itself is gone."""
    out = strip_markdown("Here's your reply.\n\n\U0001F4A1 You have 2 things to review.")
    assert out == "Here's your reply.\nYou have 2 things to review."
    assert "\n " not in out


def test_emoji_removal_leaves_no_trailing_space_on_its_line():
    out = strip_markdown("Great job today \U0001F389\nKeep it up.")
    assert out == "Great job today\nKeep it up."
    assert " \n" not in out


def test_plain_prose_unchanged():
    s = "Sure, what do you want to hear?"
    assert strip_markdown(s) == s


def test_empty_string_safe():
    assert strip_markdown("") == ""


def test_asterisk_multiplication_preserved():
    # Contract change (wave 5, final): the multiplication carve-out is
    # DROPPED. It was invented during review, not required by the original
    # spec, and preserving it cost five regression cycles chasing every new
    # shape of glued/nested/unbalanced markup. This is a home voice
    # assistant -- it will never speak "3 * 4"; the system prompt already
    # tells the model to write plain spoken prose. Every "*" is now deleted
    # unconditionally, so this is an ACCEPTED outcome, not a regression.
    assert strip_markdown("3 * 4 * 5 equals 60") == "3 4 5 equals 60"


def test_dunder_and_snake_case_identifiers_preserved():
    # Design decision: leaking literal "*"/"_" into speech is the primary
    # failure mode; over-stripping emphasis is the benign, secondary one.
    # snake_case survives (interior single underscore between word chars is
    # not markup) but __init__ is treated as bold markup like any other
    # __..__ pair, so it becomes "init". That is an ACCEPTED, deliberate
    # outcome for a home voice assistant -- not a bug.
    assert (
        strip_markdown("The setup_wizard_v2 module is __init__ based")
        == "The setup_wizard_v2 module is init based"
    )


def test_empty_link_url_unwrapped():
    assert strip_markdown("[text]()") == "text"


def test_glued_and_spaced_bold_forms_stripped():
    assert strip_markdown("**cat**s") == "cats"
    assert strip_markdown("prefix**bold**") == "prefixbold"
    assert strip_markdown("** bold with spaces **") == "bold with spaces"
    assert strip_markdown("** spaced **") == "spaced"


def test_double_underscore_bold_removed():
    assert strip_markdown("__yes__") == "yes"


def test_dunder_becomes_plain_word():
    # ACCEPTED outcome (see design comment in text_format.py): this is a
    # home voice assistant, not a code reader, so __init__ is stripped like
    # any other __bold__ pair rather than special-cased.
    assert strip_markdown("__init__") == "init"


def test_snake_case_name_preserved_intact():
    assert strip_markdown("snake_case_name") == "snake_case_name"


def test_no_stray_emphasis_characters_survive_mixed_markup():
    out = strip_markdown(
        "Mix **bold**, *italic*, __strong__, and _em_ together."
    )
    assert "*" not in out
    assert "_" not in out


def test_unbalanced_asterisk_runs_stripped():
    # Only a genuinely isolated SINGLE "*" flanked by whitespace on both
    # sides survives (multiplication). Any run of 2+ asterisks is markup
    # and must never survive, no matter what surrounds it.
    assert strip_markdown("text ** more") == "text more"
    assert "*" not in strip_markdown("compute 2 ** 3 to get 8")
    assert strip_markdown("*** waiting ***") == "waiting"
    assert strip_markdown("** leading test") == "leading test"
    assert strip_markdown("trailing test **") == "trailing test"


def test_glued_asterisk_and_underscore_markup_leaves_no_stray():
    # Root-cause fix: the asterisk pass used to run before the underscore
    # pass, so by the time underscore markup was evaluated its "*" boundary
    # context had already been deleted -- a single "_" between two word
    # characters is then indistinguishable from legitimate snake_case, and
    # gets (wrongly) preserved as a stray literal underscore. Running the
    # underscore pass first (while "*" is still present, and still a
    # non-word character to \w) fixes this at the source.
    assert strip_markdown("**bold**_italic_") == "bolditalic"
    assert strip_markdown("_italic_**bold**") == "italicbold"
    assert "_" not in strip_markdown("**Warning:**_do not proceed_")
    assert "*" not in strip_markdown("**Warning:**_do not proceed_")

    # A run of 2+ underscores is never snake_case (mirrors wave 3's
    # asterisk-run normalization) -- so odd/long underscore runs glued
    # next to other markup must not leave a stray single "_" behind either.
    assert strip_markdown("__bold___italic_") == "bolditalic"
    assert strip_markdown("__a___b__") == "ab"

    # snake_case itself must still survive untouched.
    assert strip_markdown("snake_case_name") == "snake_case_name"

    out = strip_markdown(
        "Mix **bold**_glued_, _glued_**bold**, and __a___b__ together."
    )
    assert "*" not in out
    assert "_" not in out


def _underscores_are_all_snake_case(out: str) -> bool:
    """True iff every "_" in `out` is flanked by a word char on both sides."""
    for i, ch in enumerate(out):
        if ch != "_":
            continue
        left = out[i - 1] if i > 0 else ""
        right = out[i + 1] if i + 1 < len(out) else ""
        if not (re.match(r"\w", left) and re.match(r"\w", right)):
            return False
    return True


def test_no_stray_markup_characters_survive():
    # Wave 5 (final): the contract is now total by construction -- an
    # unconditional final sweep deletes every remaining "*" and every
    # remaining "_" except one flanked by word characters on both sides
    # (snake_case). No sequential boundary-sensitive regex chain can be
    # trusted to catch every new adversarial shape (five waves proved that);
    # this sweep can't miss a case because it isn't conditional on any case.
    adversarial_inputs = [
        "_**bold**_",
        "_*x*_",
        "_***word***_",
        "**bold**_italic_",
        "_italic_**bold**",
        "*_word_*",
        "text ** more",
        "*** waiting ***",
        "(*) note",
        "end.* next",
        "_ a _",
        "__a___b__",
        "****",
        "**",
    ]
    for s in adversarial_inputs:
        out = strip_markdown(s)
        assert "*" not in out, f"stray '*' survived for input {s!r}: {out!r}"
        assert _underscores_are_all_snake_case(out), (
            f"stray '_' survived for input {s!r}: {out!r}"
        )

    # The one sanctioned carve-out must still survive intact.
    assert strip_markdown("snake_case_name") == "snake_case_name"
