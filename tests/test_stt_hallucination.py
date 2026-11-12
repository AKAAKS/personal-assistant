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


# --- bare filler utterances -------------------------------------------------
#
# With Whisper's own VAD filter now off (it was trimming word onsets that
# audio_io had already trimmed once), pure noise reaches the decoder more often
# and comes back as a stock politeness token -- a live run on 3s of noise
# produced "Thank you." Treated as a turn, that is a spurious exchange. Only a
# WHOLE utterance matching one of these is noise; the same words inside a real
# sentence are kept (see test_thank_you_mid_sentence_is_kept above).


def test_bare_thank_you_is_rejected():
    assert _is_hallucination("Thank you.")
    assert _is_hallucination("thanks")
    assert _is_hallucination("Thank you very much.")


def test_bare_filler_tokens_are_rejected():
    for text in ("Mm-hmm.", "Uh...", "um", "Hmm.", "Oh."):
        assert _is_hallucination(text), text


def test_a_command_containing_a_filler_word_is_kept():
    assert not _is_hallucination("Okay, turn the lights off")
    assert not _is_hallucination("Yeah, remind me at four")
    assert not _is_hallucination("Thanks, what's the weather?")


def test_a_bare_confirmation_is_never_treated_as_noise():
    """JARVIS requires a spoken confirmation before a destructive tool call.
    Dropping a bare "yes" as a hallucination would silently break that, so
    confirmations are deliberately excluded from the filler set."""
    for text in ("yes", "Yes.", "no", "No.", "okay", "Okay.", "yeah", "sure"):
        assert not _is_hallucination(text), text
