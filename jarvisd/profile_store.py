"""Profile store: curated facts the assistant always knows."""

import os
from pathlib import Path


class ProfileStore:
    """Stores and manages curated facts injected into every assistant call."""

    HEADER = "# JARVIS profile — facts the assistant always knows\n\n"

    def __init__(self, path: str = "data/profile.md"):
        self._path = path
        self._cache_key = None
        self._cache_content = None

    def load(self) -> str:
        """Load profile file contents; "" if missing. Cached by (mtime, size)."""
        try:
            stat = os.stat(self._path)
            key = (stat.st_mtime, stat.st_size)
        except FileNotFoundError:
            self._cache_key = None
            self._cache_content = None
            return ""

        if self._cache_key == key and self._cache_content is not None:
            return self._cache_content

        try:
            content = Path(self._path).read_text(encoding="utf-8")
            self._cache_key = key
            self._cache_content = content
            return content
        except FileNotFoundError:
            self._cache_key = None
            self._cache_content = None
            return ""

    def append(self, fact: str) -> str:
        """Append a fact to the profile file. Returns status with optional warning."""
        fact = fact.replace("\n", " ").strip()
        if not fact:
            return "Remembered: "

        path = Path(self._path)
        path.parent.mkdir(parents=True, exist_ok=True)

        if not path.exists():
            path.write_text(self.HEADER, encoding="utf-8")
            self._cache_key = None
            self._cache_content = None

        with open(self._path, "a", encoding="utf-8") as f:
            f.write(f"- {fact}\n")

        self._cache_key = None
        self._cache_content = None

        result = f"Remembered: {fact}"
        if self.estimated_tokens() > 1500:
            result += " (profile is over ~1500 tokens — consider pruning data/profile.md)"

        return result

    def estimated_tokens(self) -> int:
        """Estimate token count as len(load()) // 4."""
        return len(self.load()) // 4
