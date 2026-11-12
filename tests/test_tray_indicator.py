"""What the tray icon shows, as pure logic -- no pystray, no Pillow, no GUI.

The user's problem: when JARVIS says nothing, there is no way to tell "it heard
me and is working" from "it got nothing at all". A reply arriving already proves
it heard you, so the state that actually earns its place is the brief MISSED
flash; THINKING just removes the waiting ambiguity.

Two constraints shaped the palette. Supervisory colours were already spoken for
(grey/cyan/orange/red), so the conversational states had to avoid those hue
families entirely -- hence violet, green and magenta, with glyph and motion
carrying the rest. And at 16px motion reads far better than hue, so THINKING
pulses and MISSED blinks: red is always steady, magenta always blinks, which is
what keeps them apart at tray size rather than their hue.

Supervisory states always win. The conversational states are sub-states of
LISTENING -- they only mean anything while JARVIS is actually up and armed.
"""

from clients import activity
from clients import tray_app as T


def act(state, reason=None, ts=1000.0):
    return {"state": state, "reason": reason, "ts": ts}


# --- supervisory states win -------------------------------------------------


def test_paused_ignores_any_conversational_activity():
    """Mic is off; a leftover THINKING record must not claim otherwise."""
    assert T.effective_state(T.PAUSED, act(activity.THINKING), now=1000.0) == T.PAUSED


def test_brain_down_ignores_activity():
    assert T.effective_state(T.BRAIN_DOWN, act(activity.SPEAKING), now=1000.0) == T.BRAIN_DOWN


def test_starting_ignores_activity():
    assert T.effective_state(T.STARTING, act(activity.HEARING), now=1000.0) == T.STARTING


# --- conversational states refine LISTENING --------------------------------


def test_no_activity_is_plain_armed():
    assert T.effective_state(T.LISTENING, None, now=1000.0) == T.LISTENING


def test_hearing_is_shown_while_listening():
    assert T.effective_state(T.LISTENING, act(activity.HEARING), now=1000.0) == activity.HEARING


def test_thinking_is_shown_while_listening():
    assert T.effective_state(T.LISTENING, act(activity.THINKING), now=1000.0) == activity.THINKING


def test_speaking_is_shown_while_listening():
    assert T.effective_state(T.LISTENING, act(activity.SPEAKING), now=1000.0) == activity.SPEAKING


def test_armed_activity_reads_as_listening():
    """ARMED and LISTENING are the same thing; the icon must not flicker between
    two names for one state."""
    assert T.effective_state(T.LISTENING, act(activity.ARMED), now=1000.0) == T.LISTENING


# --- the transient and the stuck -------------------------------------------


def test_missed_shows_briefly_then_returns_to_armed():
    a = act(activity.MISSED, activity.NO_WORDS)
    assert T.effective_state(T.LISTENING, a, now=1000.0) == activity.MISSED
    assert T.effective_state(T.LISTENING, a, now=1000.0 + T.MISSED_HOLD_SEC + 0.1) == T.LISTENING


def test_thinking_expires_so_a_dead_client_cannot_freeze_it():
    """If the client crashes mid-turn the icon must stop claiming it is working."""
    a = act(activity.THINKING)
    assert T.effective_state(T.LISTENING, a, now=1000.0 + T.THINKING_MAX_SEC + 1) == T.LISTENING


def test_thinking_persists_through_a_long_tool_using_turn():
    """Multi-tool turns genuinely take tens of seconds; don't give up early."""
    a = act(activity.THINKING)
    assert T.effective_state(T.LISTENING, a, now=1000.0 + 20) == activity.THINKING


# --- the palette: no new colour may collide with a supervisory one ---------


def test_conversational_colours_avoid_every_supervisory_colour():
    supervisory = {
        T.STATE_COLORS[s]
        for s in (T.STARTING, T.PAUSED, T.BRAIN_DOWN, T.ERROR, T.STOPPED)
    }
    for s in (activity.THINKING, activity.SPEAKING, activity.MISSED):
        assert T.STATE_COLORS[s] not in supervisory, f"{s} reuses a supervisory colour"


def test_hearing_reuses_the_armed_colour_and_differs_by_glyph():
    """Deliberately not a new hue: same cyan, filled ring."""
    assert T.STATE_COLORS[activity.HEARING] == T.STATE_COLORS[T.LISTENING]
    assert T.is_filled(activity.HEARING) is True
    assert T.is_filled(T.LISTENING) is False


def test_brain_down_and_client_error_are_no_longer_the_same_colour():
    """They were both (200,40,40), so you could not tell which half had broken."""
    assert T.STATE_COLORS[T.BRAIN_DOWN] != T.STATE_COLORS[T.ERROR]


def test_every_state_has_a_colour_and_a_label():
    for s in (T.STARTING, T.LISTENING, T.PAUSED, T.BRAIN_DOWN, T.ERROR, T.STOPPED,
              activity.HEARING, activity.THINKING, activity.SPEAKING, activity.MISSED):
        assert s in T.STATE_COLORS, f"{s} has no colour"
        assert s in T.STATE_LABEL, f"{s} has no tooltip label"


# --- motion: thinking pulses, missed blinks -------------------------------


def test_thinking_pulse_varies_over_time():
    """A static colour change is easy to miss at 16px; motion is not."""
    levels = {round(T.brightness(activity.THINKING, elapsed=e / 10.0), 3)
              for e in range(0, int(T.PULSE_PERIOD_SEC * 10))}
    assert len(levels) > 1


def test_steady_states_do_not_pulse():
    for s in (T.LISTENING, activity.SPEAKING, T.PAUSED, T.BRAIN_DOWN):
        levels = {round(T.brightness(s, elapsed=e / 10.0), 3) for e in range(0, 20)}
        assert levels == {1.0}, f"{s} should be steady"


def test_pulse_never_fades_to_invisible():
    """It has to stay readable at the trough, not vanish and look like a bug."""
    for e in range(0, 40):
        assert T.brightness(activity.THINKING, elapsed=e / 10.0) >= T.PULSE_MIN


def test_no_words_blinks_once():
    assert T.blink_count(activity.NO_WORDS) == 1


def test_unverified_blinks_twice():
    """Two blinks = "that didn't sound like you" (echo / speaker-verify reject)."""
    assert T.blink_count(activity.UNVERIFIED) == 2


def test_blink_is_on_at_the_start_and_toggles():
    assert T.blink_on(0.0, blinks=1) is True
    assert T.blink_on(T.BLINK_ON_SEC + 0.01, blinks=1) is False


def test_second_blink_happens_only_for_two():
    """One blink must stay dark where two would light up again."""
    t = T.BLINK_ON_SEC + T.BLINK_OFF_SEC + 0.01
    assert T.blink_on(t, blinks=2) is True
    assert T.blink_on(t, blinks=1) is False
