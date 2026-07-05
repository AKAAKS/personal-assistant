"""Episodic memory: durable conversation log with FTS5 search."""

import sqlite3
from datetime import datetime


class MemoryStore:
    """Stores conversational turns and serves search/history over them."""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def record(self, role: str, text: str) -> None:
        text = text.strip()
        if not text:
            return
        ts = datetime.now().isoformat(timespec="seconds")
        self._conn.execute(
            "INSERT INTO messages (ts, role, content) VALUES (?, ?, ?)",
            (ts, role, text),
        )
        self._conn.commit()

    def search(self, query: str, limit: int = 5) -> list[sqlite3.Row]:
        quoted = '"' + query.replace('"', '""') + '"'
        try:
            return self._conn.execute(
                """
                SELECT m.ts AS ts, m.role AS role, m.content AS content
                FROM messages_fts
                JOIN messages m ON m.id = messages_fts.rowid
                WHERE messages_fts MATCH ?
                ORDER BY m.id DESC
                LIMIT ?
                """,
                (quoted, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            return self._conn.execute(
                """
                SELECT ts, role, content FROM messages
                WHERE content LIKE ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (f"%{query}%", limit),
            ).fetchall()

    def recent(self, n: int = 20) -> list[sqlite3.Row]:
        rows = self._conn.execute(
            "SELECT ts, role, content FROM messages ORDER BY id DESC LIMIT ?",
            (n,),
        ).fetchall()
        return list(reversed(rows))

    def load_history(self, max_turns: int = 10) -> list[dict]:
        rows = self.recent(max_turns * 2)
        messages: list[dict] = []
        for row in rows:
            role, content = row["role"], row["content"]
            if messages and messages[-1]["role"] == role:
                messages[-1]["content"] += "\n" + content
            else:
                messages.append({"role": role, "content": content})
        while messages and messages[0]["role"] != "user":
            messages.pop(0)
        while messages and messages[-1]["role"] != "assistant":
            messages.pop()
        return messages
