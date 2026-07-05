"""Tests for ProfileStore (curated profile facts)."""

import os
import pytest
from jarvisd.profile_store import ProfileStore


class TestProfileStoreLoadMissing:
    """Test load() returns empty string for missing file."""

    def test_load_missing_file(self, tmp_path):
        """load() returns "" if file does not exist."""
        profile_path = tmp_path / "profile.md"
        store = ProfileStore(str(profile_path))
        assert store.load() == ""


class TestProfileStoreAppend:
    """Test append() creates file and returns appropriate status."""

    def test_append_creates_file_with_header(self, tmp_path):
        """append() creates file with header if missing."""
        profile_path = tmp_path / "profile.md"
        store = ProfileStore(str(profile_path))
        result = store.append("I like coffee")

        assert profile_path.exists()
        content = profile_path.read_text(encoding="utf-8")
        assert content.startswith("# JARVIS profile — facts the assistant always knows\n\n")
        assert "- I like coffee\n" in content

    def test_append_returns_remembered_message(self, tmp_path):
        """append() returns 'Remembered: {fact}' message."""
        profile_path = tmp_path / "profile.md"
        store = ProfileStore(str(profile_path))
        result = store.append("I like coffee")

        assert result == "Remembered: I like coffee"

    def test_append_collapses_newlines(self, tmp_path):
        """append() collapses newlines in fact to single spaces."""
        profile_path = tmp_path / "profile.md"
        store = ProfileStore(str(profile_path))
        fact = "I like\ncoffee\nand\ntea"
        result = store.append(fact)

        content = profile_path.read_text(encoding="utf-8")
        assert "- I like coffee and tea\n" in content
        assert result == "Remembered: I like coffee and tea"

    def test_append_strips_whitespace(self, tmp_path):
        """append() strips leading/trailing whitespace from fact."""
        profile_path = tmp_path / "profile.md"
        store = ProfileStore(str(profile_path))
        result = store.append("  I like coffee  ")

        content = profile_path.read_text(encoding="utf-8")
        assert "- I like coffee\n" in content
        assert result == "Remembered: I like coffee"

    def test_append_creates_parent_directories(self, tmp_path):
        """append() creates parent directories if they don't exist."""
        profile_path = tmp_path / "subdir" / "nested" / "profile.md"
        store = ProfileStore(str(profile_path))
        result = store.append("I like coffee")

        assert profile_path.exists()
        assert "- I like coffee\n" in profile_path.read_text(encoding="utf-8")

    def test_append_multiple_facts(self, tmp_path):
        """append() adds multiple facts to the same file."""
        profile_path = tmp_path / "profile.md"
        store = ProfileStore(str(profile_path))

        store.append("I like coffee")
        store.append("I prefer mornings")

        content = profile_path.read_text(encoding="utf-8")
        assert "- I like coffee\n" in content
        assert "- I prefer mornings\n" in content


class TestProfileStoreMtimeCaching:
    """Test mtime-based caching for load()."""

    def test_mtime_cache_reads_changed_file(self, tmp_path):
        """load() re-reads file when mtime changes."""
        profile_path = tmp_path / "profile.md"
        store = ProfileStore(str(profile_path))

        # First append
        store.append("First fact")
        first_load = store.load()
        assert "First fact" in first_load

        # Write directly to file (simulating external edit)
        with open(profile_path, "a", encoding="utf-8") as f:
            f.write("- Second fact\n")

        # Force mtime change by setting it explicitly
        stat = os.stat(profile_path)
        os.utime(profile_path, (stat.st_atime, stat.st_mtime + 1))

        # load() should pick up the new content
        second_load = store.load()
        assert "Second fact" in second_load

    def test_mtime_cache_skips_unchanged_file(self, tmp_path):
        """load() uses cache when mtime hasn't changed."""
        profile_path = tmp_path / "profile.md"
        store = ProfileStore(str(profile_path))

        store.append("Fact one")
        first_load = store.load()

        # Call load again without changing file
        second_load = store.load()

        assert first_load == second_load

    def test_cache_cleared_on_missing_file(self, tmp_path):
        """load() clears cache when file is missing, picks it up when created."""
        profile_path = tmp_path / "profile.md"
        store = ProfileStore(str(profile_path))

        # First load when missing
        assert store.load() == ""

        # Create file via append
        store.append("New fact")

        # load() should pick up the newly created file
        content = store.load()
        assert "New fact" in content
        assert "# JARVIS profile" in content


class TestProfileStoreEncoding:
    """Test UTF-8 handling regardless of platform default encoding."""

    def test_append_and_load_roundtrip_non_cp1252_chars(self, tmp_path):
        """append() + load() round-trip a fact with chars outside cp1252."""
        profile_path = tmp_path / "profile.md"
        store = ProfileStore(str(profile_path))

        result = store.append("User likes emoji 😀 and café")

        assert result == "Remembered: User likes emoji 😀 and café"
        content = store.load()
        assert "- User likes emoji 😀 and café\n" in content

    def test_load_externally_written_utf8_file(self, tmp_path):
        """load() reads an externally-written UTF-8 file with non-cp1252 chars."""
        profile_path = tmp_path / "profile.md"
        profile_path.write_text(
            "# JARVIS profile — facts the assistant always knows\n\n- likes 😀 café\n",
            encoding="utf-8",
        )
        store = ProfileStore(str(profile_path))

        content = store.load()
        assert "- likes 😀 café\n" in content


class TestProfileStoreSizeCacheKey:
    """Test that the cache key includes file size, not mtime alone."""

    def test_same_mtime_different_size_is_picked_up(self, tmp_path):
        """An external edit sharing the cached mtime but changing size is re-read."""
        profile_path = tmp_path / "profile.md"
        profile_path.write_text("old content\n", encoding="utf-8")
        stat = os.stat(profile_path)
        original_times = (stat.st_atime, stat.st_mtime)

        store = ProfileStore(str(profile_path))
        assert store.load() == "old content\n"

        # External edit with different size, then force the SAME mtime as before
        profile_path.write_text("brand new longer content\n", encoding="utf-8")
        os.utime(profile_path, original_times)

        assert store.load() == "brand new longer content\n"


class TestProfileStoreTokenEstimation:
    """Test estimated_tokens() calculation."""

    def test_estimated_tokens_empty_file(self, tmp_path):
        """estimated_tokens() returns 0 for empty/missing file."""
        profile_path = tmp_path / "profile.md"
        store = ProfileStore(str(profile_path))
        assert store.estimated_tokens() == 0

    def test_estimated_tokens_calculation(self, tmp_path):
        """estimated_tokens() returns len(load()) // 4."""
        profile_path = tmp_path / "profile.md"
        store = ProfileStore(str(profile_path))
        store.append("x" * 100)

        content = store.load()
        expected = len(content) // 4
        assert store.estimated_tokens() == expected

    def test_estimated_tokens_with_multiple_facts(self, tmp_path):
        """estimated_tokens() scales with multiple facts."""
        profile_path = tmp_path / "profile.md"
        store = ProfileStore(str(profile_path))

        store.append("Fact one: " + "x" * 50)
        store.append("Fact two: " + "y" * 50)

        content = store.load()
        expected = len(content) // 4
        assert store.estimated_tokens() == expected


class TestProfileStoreOverCapWarning:
    """Test over-capacity warning when tokens exceed ~1500."""

    def test_no_warning_when_under_capacity(self, tmp_path):
        """append() returns no warning when under 1500 tokens."""
        profile_path = tmp_path / "profile.md"
        store = ProfileStore(str(profile_path))

        result = store.append("I like coffee")
        assert "over" not in result.lower()

    def test_warning_when_over_capacity(self, tmp_path):
        """append() adds warning when estimated_tokens() > 1500."""
        profile_path = tmp_path / "profile.md"
        store = ProfileStore(str(profile_path))

        # Create a file that will be over capacity after append
        # 1500 tokens ≈ 6000 characters needed; header is ~61 chars, so need ~5950+ chars of content
        long_fact = "x" * 6000
        profile_path.write_text(
            f"# JARVIS profile — facts the assistant always knows\n\n- {long_fact}\n",
            encoding="utf-8",
        )

        result = store.append("Another fact that pushes over capacity")

        assert "over" in result.lower()
        assert "1500" in result or "consider pruning" in result

    def test_exact_warning_text(self, tmp_path):
        """append() warning contains exact required text."""
        profile_path = tmp_path / "profile.md"
        store = ProfileStore(str(profile_path))

        # Create file that will be over capacity
        long_fact = "x" * 6000
        profile_path.write_text(
            f"# JARVIS profile — facts the assistant always knows\n\n- {long_fact}\n",
            encoding="utf-8",
        )

        result = store.append("trigger warning")

        expected_warning = " (profile is over ~1500 tokens — consider pruning data/profile.md)"
        assert result.endswith(expected_warning)
