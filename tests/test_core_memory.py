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


def test_build_system_cached_prompt_plus_uncached_time_when_no_profile():
    ai = make_assistant()
    blocks = ai._build_system()
    # system prompt (cached) + current-time block (uncached, always last)
    assert len(blocks) == 2
    from jarvisd.core import SYSTEM_PROMPT
    assert blocks[0]["text"] == SYSTEM_PROMPT
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in blocks[-1]  # a value that changes every call must not be cached
    assert "current local date and time" in blocks[-1]["text"].lower()


def test_build_system_no_profile_block_when_profile_empty(tmp_path):
    profile_store = ProfileStore(path=str(tmp_path / "profile.md"))  # file doesn't exist -> ""
    ai = make_assistant(profile_store=profile_store)
    blocks = ai._build_system()
    assert len(blocks) == 2  # system prompt + time; no profile block
    assert all("What you know about the user" not in b["text"] for b in blocks)


def test_build_system_profile_cached_before_uncached_time(tmp_path):
    profile_store = ProfileStore(path=str(tmp_path / "profile.md"))
    profile_store.append("likes tea in the morning")
    ai = make_assistant(profile_store=profile_store)

    blocks = ai._build_system()

    assert len(blocks) == 3  # system prompt + profile + time
    from jarvisd.core import SYSTEM_PROMPT
    assert blocks[0]["text"] == SYSTEM_PROMPT
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}

    profile_block = blocks[1]
    assert profile_block["text"].startswith("# What you know about the user")
    assert "likes tea in the morning" in profile_block["text"]
    assert profile_block["cache_control"] == {"type": "ephemeral"}

    # time block is last and uncached, so it never invalidates the cached prefix
    assert "cache_control" not in blocks[-1]
    assert "current local date and time" in blocks[-1]["text"].lower()


def test_build_system_time_block_reflects_current_date():
    from datetime import datetime
    ai = make_assistant()
    assert str(datetime.now().year) in ai._build_system()[-1]["text"]


def test_build_system_called_fresh_reflects_profile_changes(tmp_path):
    """ProfileStore.load() is mtime-cached; _build_system() should still see edits made after construction."""
    profile_store = ProfileStore(path=str(tmp_path / "profile.md"))
    ai = make_assistant(profile_store=profile_store)
    assert len(ai._build_system()) == 2  # system prompt + time

    profile_store.append("new fact")
    assert len(ai._build_system()) == 3  # system prompt + profile + time


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




# ---------------------------------------------------------------------------
# Task 4: barge-in interruption marker
# ---------------------------------------------------------------------------


from jarvisd.core import Assistant, INTERRUPTION_NOTE


def _bare_assistant(conversation):
    a = Assistant.__new__(Assistant)  # skip __init__ (no API key / clients needed)
    a.conversation = conversation
    return a


def test_note_interruption_marks_string_assistant_turn():
    """Fast/local path assistant turns store content as a plain str (core.py ~line 251)."""
    a = _bare_assistant([
        {"role": "user", "content": "play music"},
        {"role": "assistant", "content": "I can play lots of things"},
    ])
    a.note_interruption()
    assert a.conversation[-1]["content"].endswith(INTERRUPTION_NOTE)


def test_note_interruption_marks_list_assistant_turn():
    """Claude path assistant turns store content as a list of SDK content blocks
    (core.py ~line 321-322, `assistant_content = response.content`)."""
    a = _bare_assistant([
        {"role": "assistant", "content": [{"type": "text", "text": "here you go"}]},
    ])
    a.note_interruption()
    last = a.conversation[-1]["content"]
    assert last[-1] == {"type": "text", "text": INTERRUPTION_NOTE}


def test_note_interruption_does_not_mutate_existing_list_blocks():
    """The marker must be APPENDED as a new block, never mutated onto an existing
    block object -- real anthropic SDK content blocks (TextBlock/ToolUseBlock) are
    pydantic models, not plain dicts, and mutating one in place is not the same
    as replacing it. Use the same SimpleNamespace(type=..., text=...) stand-in
    `text_block()` this test module already uses elsewhere (e.g.
    test_claude_loop_still_enforces_guard_via_execute_guarded) to mirror the
    attribute-access (`.type`/`.text`) shape real SDK blocks expose, rather than
    a dict -- so this test would catch an implementation that tries to reach
    into the block and set `.text` instead of appending."""
    block = text_block("here you go")
    a = _bare_assistant([
        {"role": "assistant", "content": [block]},
    ])
    a.note_interruption()
    content = a.conversation[-1]["content"]
    assert content[0] is block  # original block object untouched, not mutated in place
    assert block.text == "here you go"
    assert content[-1] == {"type": "text", "text": INTERRUPTION_NOTE}


def test_note_interruption_safe_with_only_tool_use_blocks():
    """The last assistant turn may hold only tool_use blocks and no text block
    (e.g. mid tool-call). note_interruption() must not corrupt it -- it appends
    a new text marker block rather than trying to locate/mutate a text block
    that doesn't exist."""
    tool_use_block = SimpleNamespace(type="tool_use", id="tu_1", name="list_devices", input={})
    a = _bare_assistant([
        {"role": "assistant", "content": [tool_use_block]},
    ])
    a.note_interruption()
    content = a.conversation[-1]["content"]
    assert content[0] is tool_use_block  # untouched
    assert content[-1] == {"type": "text", "text": INTERRUPTION_NOTE}


def test_note_interruption_targets_most_recent_assistant_turn():
    a = _bare_assistant([
        {"role": "assistant", "content": "first reply"},
        {"role": "user", "content": "and then"},
        {"role": "assistant", "content": "second reply"},
    ])
    a.note_interruption()
    assert a.conversation[0]["content"] == "first reply"          # untouched
    assert a.conversation[-1]["content"].endswith(INTERRUPTION_NOTE)


def test_note_interruption_noop_without_assistant_turn():
    a = _bare_assistant([{"role": "user", "content": "hello"}])
    a.note_interruption()  # must not raise
    assert a.conversation == [{"role": "user", "content": "hello"}]


def test_note_interruption_noop_on_empty_conversation():
    a = _bare_assistant([])
    a.note_interruption()  # must not raise
    assert a.conversation == []


def test_note_interruption_is_idempotent_string_content():
    """Calling note_interruption() twice on a string-content turn must not
    stack the marker -- the second call should be a no-op."""
    a = _bare_assistant([
        {"role": "user", "content": "play music"},
        {"role": "assistant", "content": "I can play lots of things"},
    ])
    a.note_interruption()
    after_first = a.conversation[-1]["content"]

    a.note_interruption()
    after_second = a.conversation[-1]["content"]

    assert after_first == after_second
    assert after_first.endswith(INTERRUPTION_NOTE)
    # Verify the note is not doubled
    assert after_first.count(INTERRUPTION_NOTE) == 1


def test_note_interruption_is_idempotent_list_content():
    """Calling note_interruption() twice on a list-content turn must not
    append a duplicate marker block -- the second call should be a no-op."""
    a = _bare_assistant([
        {"role": "assistant", "content": [{"type": "text", "text": "here you go"}]},
    ])
    a.note_interruption()
    after_first_content = list(a.conversation[-1]["content"])
    after_first_str = str(after_first_content)

    a.note_interruption()
    after_second_content = a.conversation[-1]["content"]
    after_second_str = str(after_second_content)

    assert after_first_str == after_second_str
    # Verify the marker block appears exactly once
    marker_count = sum(
        1 for block in after_second_content
        if isinstance(block, dict) and block.get("text") == INTERRUPTION_NOTE
    )
    assert marker_count == 1


# ---------------------------------------------------------------------------
# note_interruption(turn=...): explicit-target variant used by the server's
# capture-at-receipt barge-in fix (a stale flag consumed later must still mark
# the turn that was actually playing when the interrupt happened, not
# whatever is newest in self.conversation by consume time).
# ---------------------------------------------------------------------------


def test_note_interruption_marks_explicit_turn_not_the_newest_one():
    """Captured turn is the FIRST assistant reply; a second, newer assistant
    turn has since been appended (e.g. an HA-rule action, or another client's
    exchange). Passing the captured turn explicitly must mark that one, not
    self.conversation's current last assistant turn."""
    first_reply = {"role": "assistant", "content": "reply that was actually interrupted"}
    a = _bare_assistant([
        first_reply,
        {"role": "user", "content": "and then"},
        {"role": "assistant", "content": "unrelated later reply"},
    ])
    a.note_interruption(first_reply)
    assert first_reply["content"].endswith(INTERRUPTION_NOTE)
    assert a.conversation[-1]["content"] == "unrelated later reply"  # untouched


def test_note_interruption_none_is_indistinguishable_from_no_arg():
    """Python can't tell note_interruption(None) apart from note_interruption()
    -- both must do exactly the original walk-and-mark-most-recent thing. The
    "a stored capture of None means don't mark anything" rule from the barge-in
    fix is enforced by the SERVER (it simply skips calling note_interruption at
    all when the turn it captured at interrupt receipt was None -- see
    JarvisServer._process / test_server.py's stored-None test), not by this
    method silently no-op'ing on an explicit None."""
    a = _bare_assistant([
        {"role": "assistant", "content": "some later reply"},
    ])
    a.note_interruption(None)
    assert a.conversation[-1]["content"].endswith(INTERRUPTION_NOTE)


def test_note_interruption_explicit_turn_list_content():
    """Explicit-turn variant handles the Claude list-content shape identically
    to the no-arg walk (shared marking logic)."""
    turn = {"role": "assistant", "content": [{"type": "text", "text": "here you go"}]}
    a = _bare_assistant([turn])
    a.note_interruption(turn)
    assert turn["content"][-1] == {"type": "text", "text": INTERRUPTION_NOTE}


def test_note_interruption_explicit_turn_is_idempotent():
    turn = {"role": "assistant", "content": "here you go"}
    a = _bare_assistant([turn])
    a.note_interruption(turn)
    after_first = turn["content"]
    a.note_interruption(turn)
    assert turn["content"] == after_first
    assert turn["content"].count(INTERRUPTION_NOTE) == 1


def test_note_interruption_explicit_turn_survives_compaction():
    """A turn captured at interrupt receipt may have since been compacted out
    of self.conversation entirely (_compact_history trims MAX_HISTORY). Marking
    it must not raise -- it's a harmless no-op on the live history, since the
    caller holds its own reference to the dict."""
    turn = {"role": "assistant", "content": "long since compacted away"}
    a = _bare_assistant([{"role": "assistant", "content": "current live turn"}])
    a.note_interruption(turn)  # turn is not even in a.conversation
    assert turn["content"].endswith(INTERRUPTION_NOTE)
    assert a.conversation[-1]["content"] == "current live turn"  # untouched


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
