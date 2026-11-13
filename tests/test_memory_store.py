import re

from jarvisd import db
from jarvisd.memory_store import MemoryStore


def make_store(tmp_path):
    conn = db.connect(str(tmp_path / "jarvis.db"))
    return MemoryStore(conn), conn


def test_migration_reaches_current_version_and_is_idempotent(tmp_path):
    path = str(tmp_path / "jarvis.db")
    db.connect(path).close()
    conn = db.connect(path)  # must not re-run migrations / raise
    row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    assert row["v"] == db.SCHEMA_VERSION
    conn.close()


def test_record_ignores_empty_and_whitespace(tmp_path):
    store, conn = make_store(tmp_path)
    store.record("user", "")
    store.record("user", "   \n  ")
    count = conn.execute("SELECT COUNT(*) AS c FROM messages").fetchone()["c"]
    assert count == 0


def test_record_and_search_round_trip(tmp_path):
    store, conn = make_store(tmp_path)
    store.record("user", "what's the weather in paris")
    store.record("assistant", "it's sunny in paris")
    rows = store.search("paris")
    assert len(rows) == 2
    assert all("paris" in r["content"] for r in rows)
    assert set(rows[0].keys()) >= {"ts", "role", "content"}


def test_search_is_newest_first_and_respects_limit(tmp_path):
    store, conn = make_store(tmp_path)
    for i in range(5):
        store.record("user", f"marker message number {i}")
    rows = store.search("marker", limit=2)
    assert len(rows) == 2
    assert rows[0]["content"].endswith("4")
    assert rows[1]["content"].endswith("3")


def test_search_hostile_queries_do_not_raise(tmp_path):
    store, conn = make_store(tmp_path)
    store.record("user", "hello world")
    for query in ['weird "quotes" AND (', "foo*", '"""', "AND OR NOT", ""]:
        rows = store.search(query)
        assert isinstance(rows, list)


def test_recent_returns_chronological_order(tmp_path):
    store, conn = make_store(tmp_path)
    store.record("user", "first")
    store.record("assistant", "second")
    store.record("user", "third")
    rows = store.recent(2)
    assert [r["content"] for r in rows] == ["second", "third"]


def test_recent_default_limit(tmp_path):
    store, conn = make_store(tmp_path)
    for i in range(25):
        store.record("user" if i % 2 == 0 else "assistant", f"msg{i}")
    rows = store.recent()
    assert len(rows) == 20


def test_timestamps_are_iso_local_seconds(tmp_path):
    store, conn = make_store(tmp_path)
    store.record("user", "hi")
    rows = store.recent(1)
    ts = rows[0]["ts"]
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$", ts)


def test_load_history_empty_db_returns_empty_list(tmp_path):
    store, conn = make_store(tmp_path)
    assert store.load_history() == []


def test_load_history_merges_consecutive_same_role(tmp_path):
    store, conn = make_store(tmp_path)
    store.record("user", "hi")
    store.record("user", "actually wait")
    store.record("assistant", "sure, go ahead")
    history = store.load_history()
    # each merged user row keeps its own stamp, so a pause inside one turn shows
    assert len(history) == 2
    assert history[0]["role"] == "user"
    assert history[0]["content"].count("] ") == 2
    assert history[0]["content"].splitlines()[0].endswith("hi")
    assert history[0]["content"].splitlines()[1].endswith("actually wait")
    assert history[1] == {"role": "assistant", "content": "sure, go ahead"}


def test_load_history_drops_trailing_unanswered_user_turn(tmp_path):
    store, conn = make_store(tmp_path)
    store.record("user", "q1")
    store.record("assistant", "a1")
    store.record("user", "q2 unanswered")
    history = store.load_history()
    assert history[-1]["role"] == "assistant"
    assert len(history) == 2
    assert history[0]["role"] == "user" and history[0]["content"].endswith("q1")
    assert history[1] == {"role": "assistant", "content": "a1"}


def test_load_history_starts_with_user(tmp_path):
    store, conn = make_store(tmp_path)
    # simulate a stray leading assistant row (shouldn't normally happen, but be defensive)
    conn.execute(
        "INSERT INTO messages (ts, role, content) VALUES (?, ?, ?)",
        ("2026-01-01T00:00:00", "assistant", "stray"),
    )
    conn.commit()
    store.record("user", "q1")
    store.record("assistant", "a1")
    history = store.load_history()
    assert history[0]["role"] == "user"
    assert len(history) == 2
    assert history[0]["content"].endswith("q1")
    assert history[1] == {"role": "assistant", "content": "a1"}


def test_load_history_valid_alternation_shape(tmp_path):
    store, conn = make_store(tmp_path)
    for i in range(6):
        store.record("user", f"q{i}")
        store.record("assistant", f"a{i}")
    history = store.load_history(max_turns=3)
    roles = [m["role"] for m in history]
    assert roles[0] == "user"
    assert roles[-1] == "assistant"
    for i in range(len(roles) - 1):
        assert roles[i] != roles[i + 1]


def _insert(conn, ts, role, content):
    conn.execute("INSERT INTO messages (ts, role, content) VALUES (?, ?, ?)",
                 (ts, role, content))
    conn.commit()


def test_load_history_stamps_a_user_turn_with_when_it_was_said(tmp_path):
    """Restored turns arrive with no time of their own, so the model reads them
    as if they had just happened. Observed: a reminder set at 19:33 ("two
    minutes from now") was reported at 23:59 as still about to fire."""
    store, conn = make_store(tmp_path)
    _insert(conn, "2026-11-12T19:33:08", "user", "Remind me in two minutes about the light.")
    _insert(conn, "2026-11-12T19:33:09", "assistant", "Done - reminder set for two minutes from now.")
    history = store.load_history()
    assert history[0]["content"] == (
        "[2026-11-12 19:33] Remind me in two minutes about the light.")


def test_load_history_leaves_assistant_turns_unstamped(tmp_path):
    """Only user turns carry the marker, so the model never sees the format in
    its own voice and starts reproducing it in replies."""
    store, conn = make_store(tmp_path)
    _insert(conn, "2026-11-12T19:33:08", "user", "hello")
    _insert(conn, "2026-11-12T19:33:09", "assistant", "hi there")
    history = store.load_history()
    assert history[1] == {"role": "assistant", "content": "hi there"}


def test_load_history_survives_an_unparseable_timestamp(tmp_path):
    """A bad ts must not cost us the turn."""
    store, conn = make_store(tmp_path)
    _insert(conn, "not a timestamp", "user", "hello")
    _insert(conn, "2026-11-12T19:33:09", "assistant", "hi there")
    history = store.load_history()
    assert history[0]["content"] == "hello"
