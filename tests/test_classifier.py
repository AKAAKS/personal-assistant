"""Characterization tests: lock v1 routing behavior before refactor."""
from router.classifier import IntentClassifier, Tier

clf = IntentClassifier()


def test_greetings_go_local():
    for text in ("hey", "Hello!", "good morning", "thanks a lot", "ok cool"):
        assert clf.classify(text) == Tier.LOCAL, text


def test_simple_commands_go_haiku():
    for text in ("turn off the bedroom light", "play some jazz", "pause the music"):
        assert clf.classify(text) == Tier.HAIKU, text


def test_complex_chains_go_sonnet():
    assert clf.classify("set up movie night: dim the lights and then also open Netflix") == Tier.SONNET


def test_very_long_requests_go_sonnet():
    assert clf.classify("word " * 45) == Tier.SONNET


def test_short_non_action_goes_local():
    assert clf.classify("nice weather") == Tier.LOCAL
