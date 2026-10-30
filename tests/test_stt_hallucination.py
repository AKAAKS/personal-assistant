"""Reject Whisper's stock hallucinations so background media can't drive a
hands-free feedback loop.

With media playing in the room, Whisper invents plausible speech from the audio
bleed — overwhelmingly YouTube-style outros ("I'll see you in the next video.
Bye.") that no one ever says to an assistant. Treated as a real turn, each one
keeps the follow-up conversation alive and JARVIS loops. _is_hallucination flags
them so transcribe() returns "", which the client counts as a misfire and
re-arms the wake word.
"""

from voice.stt import _is_hallucination


def test_youtube_outro_is_rejected():
    assert _is_hallucination("I'll see you in the next video. Bye. Bye. Bye.")


def test_thanks_for_watching_is_rejected():
    assert _is_hallucination("Thanks for watching!")


def test_like_and_subscribe_is_rejected():
    assert _is_hallucination("Please like and subscribe to my channel.")


def test_repeated_bye_is_rejected():
    assert _is_hallucination("Bye. Bye. Bye.")


def test_bare_you_is_rejected():
    assert _is_hallucination("you")


def test_empty_is_rejected():
    assert _is_hallucination("")


def test_real_request_is_kept():
    assert not _is_hallucination("What's the weather tomorrow?")


def test_subscribe_as_real_request_is_kept():
    # A genuine instruction that merely contains the word isn't an outro phrase.
    assert not _is_hallucination("Add a subscribe reminder to my list")


def test_thank_you_mid_sentence_is_kept():
    assert not _is_hallucination("Thank you, now turn off the lights")
