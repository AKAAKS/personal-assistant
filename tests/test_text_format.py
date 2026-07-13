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


def test_plain_prose_unchanged():
    s = "Sure, what do you want to hear?"
    assert strip_markdown(s) == s


def test_empty_string_safe():
    assert strip_markdown("") == ""
