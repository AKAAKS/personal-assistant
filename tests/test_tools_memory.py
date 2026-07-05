"""Tests for memory tools (remember/recall) and registry integration."""

import pytest
from pathlib import Path
from types import SimpleNamespace
from jarvisd.tools import build_registry, memory
from jarvisd.profile_store import ProfileStore
from jarvisd.memory_store import MemoryStore
import sqlite3


class FakeHA:
    def get_all_entities(self):
        return [{"entity_id": "light.desk", "state": "on", "attributes": {"friendly_name": "Desk"}}]

    def get_entities_by_domain(self, domain):
        return self.get_all_entities()

    def get_entity_state(self, entity_id):
        return {"entity_id": entity_id, "state": "on", "attributes": {}}

    def call_service(self, domain, action, entity_id, data=None):
        pass

    def send_notification(self, message, title):
        pass


class FakeSystem:
    def close_app(self, name): return f"closed {name}"
    def launch_app(self, target): return f"launched {target}"
    def list_processes(self): return "procs"
    def open_url(self, url): return f"opened {url}"
    def system_info(self): return "info"


def test_memory_tools_defined():
    """Verify remember and recall tools are defined."""
    tool_names = [t["name"] for t in memory.TOOLS]
    assert "remember" in tool_names
    assert "recall" in tool_names


def test_memory_tools_schema():
    """Verify tool schemas are well-formed."""
    for tool in memory.TOOLS:
        assert "name" in tool
        assert "description" in tool
        assert "input_schema" in tool
        assert tool["input_schema"]["type"] == "object"

        if tool["name"] == "remember":
            assert "fact" in tool["input_schema"]["properties"]
            assert "fact" in tool["input_schema"]["required"]
        elif tool["name"] == "recall":
            assert "query" in tool["input_schema"]["properties"]
            assert "query" in tool["input_schema"]["required"]
            assert "limit" in tool["input_schema"]["properties"]


def test_remember_handler(tmp_path):
    """Test remember handler stores facts and returns status."""
    profile_store = ProfileStore(str(tmp_path / "profile.md"))
    memory_store = SimpleNamespace()  # not used for remember

    handlers = memory.handlers(profile_store, memory_store)
    result = handlers["remember"]({"fact": "I like pizza"})

    assert "Remembered" in result
    assert "I like pizza" in result
    assert (tmp_path / "profile.md").exists()


def test_recall_handler_no_matches(tmp_path):
    """Test recall returns proper no-match message."""
    # Create empty memory store
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY,
            ts TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE VIRTUAL TABLE messages_fts USING fts5(content, content=messages, content_rowid=id)
    """)

    memory_store = MemoryStore(conn)
    profile_store = SimpleNamespace()  # not used for recall

    handlers = memory.handlers(profile_store, memory_store)
    result = handlers["recall"]({"query": "nonexistent topic"})

    assert "No memories matching" in result
    assert "nonexistent topic" in result


def test_recall_handler_with_matches(tmp_path):
    """Test recall returns formatted matches."""
    # Create memory store with sample data
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY,
            ts TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE VIRTUAL TABLE messages_fts USING fts5(content, content=messages, content_rowid=id)
    """)
    conn.execute("""
        CREATE TRIGGER messages_ai AFTER INSERT ON messages BEGIN
            INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
        END
    """)

    memory_store = MemoryStore(conn)

    # Add test messages
    memory_store.record("user", "What's the weather like?")
    memory_store.record("assistant", "It's sunny and warm today.")

    profile_store = SimpleNamespace()

    handlers = memory.handlers(profile_store, memory_store)
    result = handlers["recall"]({"query": "weather"})

    # Should contain formatted entries
    assert "weather" in result.lower() or "sunny" in result.lower()
    assert "[" in result  # timestamp formatting
    assert "]" in result
    assert "user:" in result or "assistant:" in result


def test_recall_handler_limit(tmp_path):
    """Test recall respects limit parameter."""
    # Create memory store with multiple entries
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY,
            ts TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE VIRTUAL TABLE messages_fts USING fts5(content, content=messages, content_rowid=id)
    """)
    conn.execute("""
        CREATE TRIGGER messages_ai AFTER INSERT ON messages BEGIN
            INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
        END
    """)

    memory_store = MemoryStore(conn)

    # Add multiple messages
    for i in range(10):
        memory_store.record("user", f"Message {i}: test query")

    profile_store = SimpleNamespace()
    handlers = memory.handlers(profile_store, memory_store)

    # Test with limit=2
    result = handlers["recall"]({"query": "test", "limit": 2})

    # Count the formatted entries (rough check)
    line_count = len([l for l in result.split("\n") if l.strip() and "[" in l])
    assert line_count <= 2


def test_registry_without_stores():
    """Test registry without stores doesn't include memory tools."""
    tools, handlers = build_registry(FakeHA(), FakeSystem())
    tool_names = [t["name"] for t in tools]

    assert "remember" not in tool_names
    assert "recall" not in tool_names
    assert "list_devices" in tool_names  # sanity check


def test_registry_with_stores_includes_memory():
    """Test registry with stores includes memory tools last."""
    profile_store = ProfileStore()
    conn = sqlite3.connect(":memory:")
    memory_store = MemoryStore(conn)

    tools, handlers = build_registry(
        FakeHA(), FakeSystem(),
        profile_store=profile_store,
        memory_store=memory_store
    )

    tool_names = [t["name"] for t in tools]
    assert "remember" in tool_names
    assert "recall" in tool_names
    assert tool_names[-2:] == ["remember", "recall"]  # memory tools are last


def test_registry_with_only_profile_store():
    """Test registry with only profile_store doesn't add memory tools."""
    profile_store = ProfileStore()

    tools, handlers = build_registry(
        FakeHA(), FakeSystem(),
        profile_store=profile_store
    )

    tool_names = [t["name"] for t in tools]
    assert "remember" not in tool_names
    assert "recall" not in tool_names


def test_registry_with_optional_clients_and_stores():
    """Test registry with optional clients and stores."""
    profile_store = ProfileStore()
    conn = sqlite3.connect(":memory:")
    memory_store = MemoryStore(conn)
    spotify = SimpleNamespace()
    tv = SimpleNamespace()

    tools, handlers = build_registry(
        FakeHA(), FakeSystem(),
        spotify_client=spotify,
        tv_client=tv,
        profile_store=profile_store,
        memory_store=memory_store
    )

    tool_names = [t["name"] for t in tools]
    assert "spotify_play" in tool_names
    assert "tv_remote" in tool_names
    assert "remember" in tool_names
    assert "recall" in tool_names
    assert tool_names[-2:] == ["remember", "recall"]


def test_all_tools_have_handlers():
    """Test every tool has a corresponding handler."""
    profile_store = ProfileStore()
    conn = sqlite3.connect(":memory:")
    memory_store = MemoryStore(conn)

    tools, handlers = build_registry(
        FakeHA(), FakeSystem(),
        profile_store=profile_store,
        memory_store=memory_store
    )

    tool_names = [t["name"] for t in tools]
    assert set(tool_names) == set(handlers.keys())
    assert len(tool_names) == len(set(tool_names))  # no duplicates


def test_recall_truncates_content():
    """Test recall truncates content to 200 chars."""
    # Create memory store
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY,
            ts TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE VIRTUAL TABLE messages_fts USING fts5(content, content=messages, content_rowid=id)
    """)
    conn.execute("""
        CREATE TRIGGER messages_ai AFTER INSERT ON messages BEGIN
            INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
        END
    """)

    memory_store = MemoryStore(conn)

    # Add a very long message
    long_text = "This is a very long message. " * 20
    memory_store.record("user", long_text)

    profile_store = SimpleNamespace()
    handlers = memory.handlers(profile_store, memory_store)
    result = handlers["recall"]({"query": "very long"})

    # Extract content parts and verify truncation
    lines = [l for l in result.split("\n") if l.strip() and "[" in l]
    for line in lines:
        # Content should be after the role: part
        if ": " in line:
            content_part = line.split(": ", 1)[1]
            assert len(content_part) <= 200 + 10  # allow some wiggle room


def test_security_policy_has_memory_permissions():
    """Test security policy includes memory tool permissions."""
    from security.policy import TOOL_PERMISSIONS

    assert "remember" in TOOL_PERMISSIONS
    assert TOOL_PERMISSIONS["remember"] == "write"

    assert "recall" in TOOL_PERMISSIONS
    assert TOOL_PERMISSIONS["recall"] == "read_only"
