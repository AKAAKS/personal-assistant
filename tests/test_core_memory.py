"""Tests for Task 4: profile-aware system prompt, turn persistence, history reload."""

from types import SimpleNamespace

from jarvisd import db
from jarvisd.memory_store import MemoryStore
from jarvisd.profile_store import ProfileStore
from tests.test_tools_registry import FakeHA, FakeSystem


def make_assistant(profile_store=None, memory_store=None):
    from jarvisd.core import Assistant
    return Assistant(
        FakeHA(),
        "sk-test-not-a-real-key",
        system_client=FakeSystem(),
        profile_store=profile_store,
        memory_store=memory_store,
    )


class FakeMessages:
    """Stands in for anthropic Client().messages — records calls, replays canned responses."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._responses.pop(0)


class FakeAnthropicClient:
    def __init__(self, responses):
        self.messages = FakeMessages(responses)


def text_block(text):
    return SimpleNamespace(type="text", text=text)


def fake_response(text, input_tokens=10, output_tokens=5):
    return SimpleNamespace(
        content=[text_block(text)],
        usage=SimpleNamespace(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        ),
    )


class RaisingMemoryStore:
    """memory_store whose record() always raises, to prove persistence failures are swallowed."""

    def load_history(self, max_turns=10):
        return []

    def record(self, role, text):
        raise RuntimeError("boom")


# ---------------------------------------------------------------------------
# _build_system: profile block placement + cache_control
# ---------------------------------------------------------------------------


def test_build_system_single_block_when_no_profile_store():
    ai = make_assistant()
    blocks = ai._build_system()
    assert len(blocks) == 1
    assert blocks[0]["text"] == __import__("jarvisd.core", fromlist=["SYSTEM_PROMPT"]).SYSTEM_PROMPT
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}


def test_build_system_single_block_when_profile_empty(tmp_path):
    profile_store = ProfileStore(path=str(tmp_path / "profile.md"))  # file doesn't exist -> ""
    ai = make_assistant(profile_store=profile_store)
    blocks = ai._build_system()
    assert len(blocks) == 1


def test_build_system_profile_block_present_last_with_cache_control(tmp_path):
    profile_store = ProfileStore(path=str(tmp_path / "profile.md"))
    profile_store.append("likes tea in the morning")
    ai = make_assistant(profile_store=profile_store)

    blocks = ai._build_system()

    assert len(blocks) == 2
    from jarvisd.core import SYSTEM_PROMPT
    assert blocks[0]["text"] == SYSTEM_PROMPT
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}

    profile_block = blocks[-1]
    assert profile_block["text"].startswith("# What you know about the user")
    assert "likes tea in the morning" in profile_block["text"]
    assert profile_block["cache_control"] == {"type": "ephemeral"}


def test_build_system_called_fresh_reflects_profile_changes(tmp_path):
    """ProfileStore.load() is mtime-cached; _build_system() should still see edits made after construction."""
    profile_store = ProfileStore(path=str(tmp_path / "profile.md"))
    ai = make_assistant(profile_store=profile_store)
    assert len(ai._build_system()) == 1

    profile_store.append("new fact")
    assert len(ai._build_system()) == 2


# ---------------------------------------------------------------------------
# Turn persistence
# ---------------------------------------------------------------------------


def test_local_tier_persists_user_and_assistant(tmp_path):
    conn = db.connect(str(tmp_path / "jarvis.db"))
    memory_store = MemoryStore(conn)
    ai = make_assistant(memory_store=memory_store)

    reply = ai.process("hey")

    rows = memory_store.recent()
    assert [dict(role=r["role"], content=r["content"]) for r in rows] == [
        {"role": "user", "content": "hey"},
        {"role": "assistant", "content": reply},
    ]


def test_claude_tier_persists_user_and_assistant(tmp_path):
    conn = db.connect(str(tmp_path / "jarvis.db"))
    memory_store = MemoryStore(conn)
    ai = make_assistant(memory_store=memory_store)
    ai.client = FakeAnthropicClient([fake_response("Sure, turning it on.")])

    message = "Turn on the desk lamp please"
    from router.classifier import IntentClassifier, Tier
    assert IntentClassifier().classify(message) != Tier.LOCAL  # sanity: exercises the Claude path

    reply = ai.process(message)

    assert reply == "Sure, turning it on."
    rows = memory_store.recent()
    assert [dict(role=r["role"], content=r["content"]) for r in rows] == [
        {"role": "user", "content": message},
        {"role": "assistant", "content": "Sure, turning it on."},
    ]


def test_record_exception_does_not_break_reply(caplog):
    ai = make_assistant(memory_store=RaisingMemoryStore())

    reply = ai.process("hey")

    assert reply == "Hey! What can I do for you?"


def test_no_persistence_when_memory_store_is_none():
    ai = make_assistant()  # memory_store=None
    # Should not raise, and there's nothing to assert against a store — just exercise the path.
    reply = ai.process("hey")
    assert reply == "Hey! What can I do for you?"


# ---------------------------------------------------------------------------
# History reload on construction
# ---------------------------------------------------------------------------


def test_history_restored_on_construction(tmp_path, caplog):
    conn = db.connect(str(tmp_path / "jarvis.db"))
    seed_store = MemoryStore(conn)
    seed_store.record("user", "q1")
    seed_store.record("assistant", "a1")
    seed_store.record("user", "q2")
    seed_store.record("assistant", "a2")

    memory_store = MemoryStore(conn)
    with caplog.at_level("INFO"):
        ai = make_assistant(memory_store=memory_store)

    expected = memory_store.load_history(max_turns=10)
    assert ai.conversation == expected
    assert len(expected) == 4
    assert any("Restored" in r.message and "4" in r.message for r in caplog.records)


def test_history_restore_skipped_and_no_log_when_no_memory_store(caplog):
    with caplog.at_level("INFO"):
        ai = make_assistant()
    assert ai.conversation == []
    assert not any("Restored" in r.message for r in caplog.records)


def test_restored_history_valid_alternation(tmp_path):
    conn = db.connect(str(tmp_path / "jarvis.db"))
    seed_store = MemoryStore(conn)
    # consecutive user rows must merge per MemoryStore.load_history's own contract
    seed_store.record("user", "hi")
    seed_store.record("user", "actually wait")
    seed_store.record("assistant", "sure, go ahead")
    seed_store.record("user", "trailing unanswered")

    memory_store = MemoryStore(conn)
    ai = make_assistant(memory_store=memory_store)

    roles = [m["role"] for m in ai.conversation]
    assert roles[0] == "user"
    assert roles[-1] == "assistant"
    for i in range(len(roles) - 1):
        assert roles[i] != roles[i + 1]
