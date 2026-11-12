"""The prompt's capability list must describe the tools that are ACTUALLY registered.

Why this file exists: on 2026-11-03 the Google OAuth grant expired, bootstrap
caught the RefreshError and set google=None, so Gmail and Calendar tools were
never registered -- but SYSTEM_PROMPT went on advertising both for nine days.
The model, handed a prompt that promised Gmail and a tool list without it,
confabulated its way across the gap ("I don't have access to Gmail. I can only
work with your Google Calendar..."). A prompt that disagrees with the tool list
is a lie the model has to improvise around, and it improvises badly.

So: every capability line is gated on the tools it describes.
"""

from types import SimpleNamespace

from tests.test_tools_registry import FakeHA, FakeSystem


def make_assistant(**kwargs):
    from jarvisd.core import Assistant
    return Assistant(
        FakeHA(),
        "sk-test-not-a-real-key",
        system_client=FakeSystem(),
        **kwargs,
    )


def prompt_text(ai):
    """The full system prompt the model actually receives."""
    return " ".join(b["text"] for b in ai._build_system())


# -- absent tools must not be advertised ------------------------------------

def test_gmail_not_advertised_when_google_client_absent():
    """The nine-day bug: no google_client means no Gmail tools, so no Gmail claim."""
    text = prompt_text(make_assistant()).lower()
    assert "gmail" not in text


def test_calendar_not_advertised_when_google_client_absent():
    text = prompt_text(make_assistant()).lower()
    assert "calendar" not in text


def test_spotify_not_advertised_when_spotify_client_absent():
    text = prompt_text(make_assistant()).lower()
    assert "spotify" not in text


# -- present tools must still be advertised ---------------------------------

def test_gmail_advertised_when_google_client_present():
    ai = make_assistant(google_client=SimpleNamespace())
    text = prompt_text(ai).lower()
    assert "gmail" in text


def test_cannot_send_caveat_survives_when_gmail_present():
    """The curated warning is the point of hand-writing these lines -- keep it."""
    ai = make_assistant(google_client=SimpleNamespace())
    assert "CANNOT send" in prompt_text(ai)


def test_pc_control_always_advertised():
    """ha + system tools are unconditional, so their lines are too."""
    assert "PC control" in prompt_text(make_assistant())


# -- the invariant that keeps this from rotting -----------------------------

def test_every_capability_gate_names_a_real_tool():
    """A renamed or deleted tool must fail loudly here, not silently drop a
    capability line from the prompt months later."""
    from jarvisd.core import CAPABILITIES, OPERATING_NOTES, SERVER_SIDE_TOOLS
    from jarvisd.tools import build_registry

    all_tools, _ = build_registry(
        FakeHA(), FakeSystem(),
        spotify_client=SimpleNamespace(), tv_client=SimpleNamespace(),
        profile_store=SimpleNamespace(), memory_store=SimpleNamespace(),
        reminder_store=SimpleNamespace(), rules_store=SimpleNamespace(),
        meal_store=SimpleNamespace(), file_tools=True,
        proposal_store=SimpleNamespace(), google_client=SimpleNamespace(),
        location_provider=SimpleNamespace(), food_service=SimpleNamespace(),
        pantry_store=SimpleNamespace(), meal_log_conn=SimpleNamespace(),
    )
    # SERVER_SIDE_TOOLS (web_search) are supplied per-request by _call_model,
    # not by the registry, so they are legitimate gates too.
    known = {t["name"] for t in all_tools} | set(SERVER_SIDE_TOOLS)

    for line, required in CAPABILITIES + OPERATING_NOTES:
        unknown = set(required) - known
        assert not unknown, f"line gates on missing tool(s) {unknown}: {line[:60]}"


def test_every_capability_line_gates_on_at_least_one_tool():
    """An ungated line is a hardcoded claim -- exactly what this change removes."""
    from jarvisd.core import CAPABILITIES, OPERATING_NOTES

    for line, required in CAPABILITIES + OPERATING_NOTES:
        assert required, f"line has no tool gate: {line[:60]}"


def test_spotify_operating_note_dropped_with_spotify():
    """The 'search for the URI first' instruction is useless without the tools."""
    assert "search for the URI first" not in prompt_text(make_assistant())


# -- directions ------------------------------------------------------------
#
# On 2026-10-30 JARVIS answered "walk from my home to a nearby gym" by building
# a https://www.google.com/maps/dir/<origin>/<destination> URL -- taking the
# origin from the coordinates in its own location block -- and opening it.
# On 2026-11-12 it refused the same request, saying it could only
# open Google Maps for the user to fill in. Nothing had changed in the code:
# the prompt never granted a directions capability, so the Oct 30 answer was
# improvisation that a clean context produced ~4 times in 5 and a context
# containing its own "I can only open URLs" produced 0 times in 5.
#
# Granting the capability explicitly beat the poisoned transcript 6/6.

def test_directions_capability_rendered_when_browser_tool_available():
    from jarvisd.core import render_system_prompt
    assert "maps/dir" in render_system_prompt({"open_url_browser"})


def test_directions_capability_absent_without_browser_tool():
    from jarvisd.core import render_system_prompt
    assert "maps/dir" not in render_system_prompt({"launch_app"})


def test_directions_capability_names_travelmode():
    """Without travelmode the URL defaults to driving, so 'walking directions'
    would silently return a driving route -- the original question."""
    text = render_capability_text()
    assert "travelmode" in text
    assert "walking" in text


def render_capability_text():
    from jarvisd.core import render_system_prompt
    return render_system_prompt({"open_url_browser"})
