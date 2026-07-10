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


def fake_response(text, input_tokens=10, output_tokens=5, stop_reason="end_turn", server_tool_use=None):
    return SimpleNamespace(
        content=[text_block(text)],
        stop_reason=stop_reason,
        usage=SimpleNamespace(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
            server_tool_use=server_tool_use,
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


# ---------------------------------------------------------------------------
# execute_guarded: shared guard/audit/execute choke point (Task 6)
# ---------------------------------------------------------------------------


class SpyAudit:
    def __init__(self):
        self.calls = []

    def log(self, tool_name, tool_input, verdict, reason, result=None):
        self.calls.append((tool_name, tool_input, verdict, reason, result))


def test_execute_guarded_block_short_circuits_handler_and_audits_blocked():
    ai = make_assistant()
    handler_calls = []
    ai._handlers["send_notification"] = lambda inp: handler_calls.append(inp) or "sent"
    spy_audit = SpyAudit()
    ai.audit = spy_audit

    result = ai.execute_guarded("send_notification", {"message": "my ssn is 123"})

    assert result.startswith("SECURITY BLOCK:")
    assert handler_calls == []  # guard short-circuited before the handler ran
    assert len(spy_audit.calls) == 1
    tool_name, tool_input, verdict, reason, logged_result = spy_audit.calls[0]
    assert (tool_name, tool_input, verdict) == ("send_notification", {"message": "my ssn is 123"}, "blocked")
    assert logged_result is None


def test_execute_guarded_confirm_required_does_not_execute_and_audits():
    """THE safety property: a destructive tool the guard flags CONFIRM_REQUIRED
    must never auto-execute -- execute_guarded returns the confirmation prompt,
    the handler is not called, and the audit records 'confirm_required'."""
    from security.guard import SecurityResult, Verdict

    ai = make_assistant()
    handler_calls = []
    ai._handlers["control_switch"] = lambda inp: handler_calls.append(inp) or "toggled"
    ai.guard = SimpleNamespace(
        check=lambda name, inp, autonomous=False: SecurityResult(
            Verdict.CONFIRM_REQUIRED, "needs confirmation"
        )
    )
    spy_audit = SpyAudit()
    ai.audit = spy_audit

    result = ai.execute_guarded("control_switch", {"entity_id": "switch.heater"})

    assert result == "CONFIRMATION REQUIRED: needs confirmation"
    assert handler_calls == []  # destructive action did NOT auto-execute
    assert len(spy_audit.calls) == 1
    tool_name, tool_input, verdict, reason, logged_result = spy_audit.calls[0]
    assert (tool_name, tool_input, verdict) == ("control_switch", {"entity_id": "switch.heater"}, "confirm_required")
    assert logged_result is None


def test_execute_guarded_allowed_calls_handler_and_audits_allowed():
    ai = make_assistant()
    spy_audit = SpyAudit()
    ai.audit = spy_audit

    result = ai.execute_guarded("list_devices", {})

    assert "Desk" in result
    assert len(spy_audit.calls) == 1
    tool_name, tool_input, verdict, reason, logged_result = spy_audit.calls[0]
    assert (tool_name, tool_input, verdict) == ("list_devices", {}, "allowed")
    assert logged_result == result


def test_claude_loop_still_enforces_guard_via_execute_guarded():
    """_claude_loop must route every tool call through execute_guarded -- not a
    parallel, hand-inlined copy of the guard/audit/execute sequence."""
    ai = make_assistant()
    calls = []
    original = ai.execute_guarded

    def spy(tool_name, tool_input, autonomous=False):
        calls.append((tool_name, tool_input))
        return original(tool_name, tool_input, autonomous=autonomous)

    ai.execute_guarded = spy

    tool_use = SimpleNamespace(type="tool_use", id="tu_1", name="list_devices", input={})
    first_response = SimpleNamespace(
        content=[tool_use],
        stop_reason="tool_use",
        usage=SimpleNamespace(
            input_tokens=10, output_tokens=5,
            cache_read_input_tokens=0, cache_creation_input_tokens=0,
        ),
    )
    ai.client = FakeAnthropicClient([first_response, fake_response("Here's your devices.")])

    reply = ai._claude_loop("what's on?", "claude-sonnet-5")

    assert reply == "Here's your devices."
    assert calls == [("list_devices", {})]


# ---------------------------------------------------------------------------
# Task 5: server-side web search
# ---------------------------------------------------------------------------


class SpyCostTracker:
    """Minimal cost_tracker stand-in for web-search tests: tunable allow/deny, spies on record()."""

    def __init__(self, allowed=True):
        self._allowed = allowed
        self.record_calls = []

    def web_search_allowed(self):
        return self._allowed

    def record(self, *args, **kwargs):
        self.record_calls.append((args, kwargs))
        return 0.0

    def should_force_downgrade(self):
        return False


def test_web_search_tool_variant_per_model():
    from jarvisd.core import (
        HAIKU_MODEL,
        SONNET_MODEL,
        WEB_SEARCH_MAX_USES_PER_REQUEST,
        web_search_tool,
    )

    sonnet_tool = web_search_tool(SONNET_MODEL)
    assert sonnet_tool["type"] == "web_search_20260209"
    assert sonnet_tool["name"] == "web_search"
    assert sonnet_tool["max_uses"] == WEB_SEARCH_MAX_USES_PER_REQUEST

    haiku_tool = web_search_tool(HAIKU_MODEL)
    assert haiku_tool["type"] == "web_search_20250305"
    assert haiku_tool["name"] == "web_search"
    assert haiku_tool["max_uses"] == WEB_SEARCH_MAX_USES_PER_REQUEST


def test_web_search_appended_after_cached_local_tools():
    from jarvisd.core import SONNET_MODEL

    ai = make_assistant()
    ai.enable_web_search = True
    ai.cost_tracker = SpyCostTracker(allowed=True)
    fake_client = FakeAnthropicClient([fake_response("done")])
    ai.client = fake_client

    ai._claude_loop("what's the weather", SONNET_MODEL)

    captured_tools = fake_client.messages.calls[0]["tools"]
    assert captured_tools[-1]["name"] == "web_search"
    assert captured_tools[-2].get("cache_control") == {"type": "ephemeral"}


def test_web_search_dropped_when_cap_hit():
    from jarvisd.core import SONNET_MODEL

    ai = make_assistant()
    ai.enable_web_search = True
    ai.cost_tracker = SpyCostTracker(allowed=False)
    fake_client = FakeAnthropicClient([fake_response("done")])
    ai.client = fake_client

    ai._claude_loop("what's the weather", SONNET_MODEL)

    captured_tools = fake_client.messages.calls[0]["tools"]
    assert all(t.get("name") != "web_search" for t in captured_tools)


def test_server_tool_usage_recorded():
    from jarvisd.core import SONNET_MODEL

    ai = make_assistant()
    spy = SpyCostTracker(allowed=True)
    ai.cost_tracker = spy
    ai.client = FakeAnthropicClient([
        fake_response("done", server_tool_use=SimpleNamespace(web_search_requests=2))
    ])

    ai._claude_loop("what's the weather", SONNET_MODEL)

    assert spy.record_calls[-1][1]["web_search_requests"] == 2


def test_pause_turn_continues_loop():
    from jarvisd.core import SONNET_MODEL

    ai = make_assistant()
    first = fake_response("partial", stop_reason="pause_turn")
    second = fake_response("final answer")
    fake_client = FakeAnthropicClient([first, second])
    ai.client = fake_client

    reply = ai._claude_loop("search something current", SONNET_MODEL)

    assert reply == "final answer"
    assert len(fake_client.messages.calls) == 2


def blocks_response(blocks, stop_reason="end_turn"):
    """Fake API response with arbitrary content blocks (for server-side / tool_use mixes)."""
    return SimpleNamespace(
        content=list(blocks),
        stop_reason=stop_reason,
        usage=SimpleNamespace(
            input_tokens=10,
            output_tokens=5,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
            server_tool_use=None,
        ),
    )


def test_pause_turn_with_tool_use_still_executes_tools():
    """pause_turn alongside local tool_use blocks must NOT skip tool execution:
    the blocks go through execute_guarded and tool_results are appended, else the
    conversation carries dangling tool_use into the next request (API 400)."""
    from jarvisd.core import SONNET_MODEL

    ai = make_assistant()
    calls = []
    ai.execute_guarded = (
        lambda name, inp, autonomous=False: calls.append((name, inp)) or "devices: Desk"
    )

    tool_use = SimpleNamespace(type="tool_use", id="tu_pause", name="list_devices", input={})
    first = blocks_response([tool_use], stop_reason="pause_turn")
    fake_client = FakeAnthropicClient([first, fake_response("done after tools")])
    ai.client = fake_client

    reply = ai._claude_loop("check devices", SONNET_MODEL)

    assert reply == "done after tools"
    assert calls == [("list_devices", {})]
    assert len(fake_client.messages.calls) == 2
    # the tool_result for tu_pause was appended as the follow-up user message
    tool_result_blocks = [
        b
        for m in ai.conversation
        if m["role"] == "user" and isinstance(m["content"], list)
        for b in m["content"]
        if isinstance(b, dict) and b.get("type") == "tool_result"
    ]
    assert [b["tool_use_id"] for b in tool_result_blocks] == ["tu_pause"]


def test_server_side_blocks_ignored_by_loop():
    """server_tool_use / web_search_tool_result blocks are neither text nor tool_use:
    the loop must return just the text and never try to execute them locally."""
    from jarvisd.core import SONNET_MODEL

    ai = make_assistant()
    calls = []
    ai.execute_guarded = (
        lambda name, inp, autonomous=False: calls.append((name, inp)) or "unexpected"
    )
    content = [
        SimpleNamespace(type="server_tool_use", id="srvtoolu_1", name="web_search", input={"query": "weather"}),
        SimpleNamespace(type="web_search_tool_result", tool_use_id="srvtoolu_1", content=[]),
        text_block("It's sunny today."),
    ]
    ai.client = FakeAnthropicClient([blocks_response(content)])

    reply = ai._claude_loop("what's the weather", SONNET_MODEL)

    assert reply == "It's sunny today."
    assert calls == []


def test_pause_cap_fallback_when_no_text():
    """6 consecutive pause_turns exceed PAUSE_TURN_MAX_CONTINUATIONS=5; with no
    text accumulated the loop bails with the fallback string instead of spinning."""
    from jarvisd.core import PAUSE_TURN_MAX_CONTINUATIONS, SONNET_MODEL

    responses = [blocks_response([], stop_reason="pause_turn") for _ in range(6)]
    ai = make_assistant()
    fake_client = FakeAnthropicClient(responses)
    ai.client = fake_client

    reply = ai._claude_loop("search something", SONNET_MODEL)

    assert reply == "(web search did not complete)"
    assert len(fake_client.messages.calls) == PAUSE_TURN_MAX_CONTINUATIONS + 1


def test_pause_cap_returns_text_when_present():
    """Same cap trip, but the pause responses carried text: return it rather than the fallback."""
    from jarvisd.core import SONNET_MODEL

    responses = [fake_response(f"chunk {i}", stop_reason="pause_turn") for i in range(6)]
    ai = make_assistant()
    fake_client = FakeAnthropicClient(responses)
    ai.client = fake_client

    reply = ai._claude_loop("search something", SONNET_MODEL)

    assert reply == "chunk 5"
    assert len(fake_client.messages.calls) == 6


# ---------------------------------------------------------------------------
# Final-review must-fix: destructive tools cannot be self-confirmed within one turn
# ---------------------------------------------------------------------------


def test_model_cannot_self_confirm_run_script_within_one_turn():
    """The destructive confirm flow arms on the first call and consumes on an
    identical retry -- but the retry must come from a *later* user turn, not from
    the model looping within the same process() turn. A single _claude_loop
    (one turn) that emits run_script twice must leave it CONFIRMATION REQUIRED
    both times and never execute the handler (otherwise a prompt-injected
    document could get a whitelisted destructive script run with no human)."""
    from jarvisd.core import SONNET_MODEL

    ai = make_assistant()
    ai.audit = SimpleNamespace(log=lambda *a, **k: None)
    executed = []
    ai._handlers["run_script"] = lambda ti: executed.append(ti) or "ran it"

    def run_script_block(uid):
        return SimpleNamespace(type="tool_use", id=uid, name="run_script", input={"name": "wipe.py"})

    fake_client = FakeAnthropicClient([
        blocks_response([run_script_block("tu_1")], stop_reason="tool_use"),
        blocks_response([run_script_block("tu_2")], stop_reason="tool_use"),
        fake_response("ok, I need you to confirm that."),
    ])
    ai.client = fake_client

    ai._claude_loop("run wipe.py", SONNET_MODEL)

    assert executed == []


def test_run_script_executes_after_a_real_turn_boundary():
    """The legitimate path still works: arm in one turn, confirm in the next.
    begin_turn() (called at the top of process()) is the turn boundary that makes
    a prior-turn confirmation consumable."""
    ai = make_assistant()
    ai.audit = SimpleNamespace(log=lambda *a, **k: None)
    executed = []
    ai._handlers["run_script"] = lambda ti: executed.append(ti) or "ran it"

    ai.guard.begin_turn()
    first = ai.execute_guarded("run_script", {"name": "x.py"})
    assert first.startswith("CONFIRMATION REQUIRED")
    assert executed == []

    ai.guard.begin_turn()  # user's next turn ("yes, run it")
    second = ai.execute_guarded("run_script", {"name": "x.py"})
    assert second == "ran it"
    assert executed == [{"name": "x.py"}]
