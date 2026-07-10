"""Tests for file tools: search_files / read_document (jarvisd/tools/files.py)."""
import sys

import pytest

from jarvisd.tools import files


@pytest.fixture(autouse=True)
def force_fallback_search(monkeypatch):
    """All tests in this module exercise the os.walk fallback, not es.exe."""
    monkeypatch.setattr(files.shutil, "which", lambda name: None)


@pytest.fixture
def file_root(tmp_path, monkeypatch):
    """A single allowed root under tmp_path, wired via FILE_ROOTS."""
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setenv("FILE_ROOTS", str(root))
    return root


@pytest.fixture
def handlers(file_root):
    """Default handlers with no summarizer."""
    return files.handlers()


class TestSearchFiles:
    def test_search_finds_file_case_insensitive(self, file_root, handlers):
        (file_root / "MyReport.txt").write_text("hello", encoding="utf-8")

        result = handlers["search_files"]({"query": "myreport"})

        assert "MyReport.txt" in result

    def test_search_respects_cap_of_30(self, file_root, handlers):
        for i in range(35):
            (file_root / f"match_{i:02d}.txt").write_text("x", encoding="utf-8")

        result = handlers["search_files"]({"query": "match"})
        lines = [l for l in result.splitlines() if l.strip()]

        assert len(lines) == 30

    def test_search_skips_node_modules(self, file_root, handlers):
        nm = file_root / "node_modules"
        nm.mkdir()
        (nm / "matchme.txt").write_text("x", encoding="utf-8")

        result = handlers["search_files"]({"query": "matchme"})

        assert "No files found matching" in result

    def test_search_drops_dot_env(self, file_root, handlers):
        (file_root / ".env").write_text("SECRET=1", encoding="utf-8")

        result = handlers["search_files"]({"query": "env"})

        assert ".env" not in result
        assert "No files found matching" in result

    def test_search_no_match_returns_friendly_string(self, file_root, handlers):
        result = handlers["search_files"]({"query": "does-not-exist-xyz"})

        assert result == "No files found matching 'does-not-exist-xyz'."

    def test_search_missing_query_returns_error(self, handlers, file_root):
        result = handlers["search_files"]({})

        assert result.startswith("Error")

    def test_search_directory_outside_allowed_roots_is_rejected(self, file_root, handlers, tmp_path):
        outside = tmp_path / "elsewhere"
        outside.mkdir()

        result = handlers["search_files"]({"query": "x", "directory": str(outside)})

        assert result.startswith("Error")


class TestReadDocument:
    def test_reads_utf8_md_with_emoji(self, file_root, handlers):
        target = file_root / "notes.md"
        target.write_text("# Notes\n\nHello world! 🎉", encoding="utf-8")

        result = handlers["read_document"]({"path": str(target)})

        assert "🎉" in result
        assert "Hello world" in result

    def test_refuses_file_over_size_cap(self, file_root, handlers):
        big = file_root / "big.txt"
        big.write_bytes(b"a" * (5 * 1024 * 1024 + 1))

        result = handlers["read_document"]({"path": str(big)})

        assert result == "Error: file too large"

    def test_missing_file_returns_error(self, file_root, handlers):
        missing = file_root / "nope.txt"

        result = handlers["read_document"]({"path": str(missing)})

        assert isinstance(result, str)
        assert result.startswith("Error")

    def test_path_outside_allowed_roots_is_rejected(self, file_root, handlers, tmp_path):
        outside = tmp_path / "outside.txt"
        outside.write_text("secret stuff", encoding="utf-8")

        result = handlers["read_document"]({"path": str(outside)})

        assert result.startswith("Error")

    def test_long_text_with_summarizer_spy(self, file_root):
        target = file_root / "long.txt"
        long_text = "word " * 2000  # well over 6000 chars
        target.write_text(long_text, encoding="utf-8")

        calls = []

        def spy_summarizer(text):
            calls.append(text)
            return "a short summary"

        handlers = files.handlers(summarizer=spy_summarizer)
        result = handlers["read_document"]({"path": str(target)})

        assert len(calls) == 1
        assert calls[0] == long_text[:30000]
        assert result == "Summary of long.txt:\na short summary"

    def test_long_text_without_summarizer_is_truncated(self, file_root, handlers):
        target = file_root / "long_no_summarizer.txt"
        long_text = "word " * 2000
        target.write_text(long_text, encoding="utf-8")

        result = handlers["read_document"]({"path": str(target)})

        assert result == long_text[:6000] + "\n...[truncated]"

    def test_short_text_returned_raw(self, file_root, handlers):
        target = file_root / "short.txt"
        target.write_text("just a short note", encoding="utf-8")

        result = handlers["read_document"]({"path": str(target)})

        assert result == "just a short note"

    def test_pdf_extraction_real_lib(self, file_root, handlers):
        pytest.importorskip("pypdf")
        from pypdf import PdfWriter

        pdf_path = file_root / "doc.pdf"
        writer = PdfWriter()
        writer.add_blank_page(width=200, height=200)
        with open(pdf_path, "wb") as f:
            writer.write(f)

        result = handlers["read_document"]({"path": str(pdf_path)})

        assert not result.startswith("Error")

    def test_docx_extraction_real_lib(self, file_root, handlers):
        pytest.importorskip("docx")
        from docx import Document

        docx_path = file_root / "doc.docx"
        doc = Document()
        doc.add_paragraph("Hello docx world with unicode café")
        doc.save(str(docx_path))

        result = handlers["read_document"]({"path": str(docx_path)})

        assert "Hello docx world" in result

    def test_pdf_missing_dependency_returns_error_string(self, file_root, handlers, monkeypatch):
        pdf_path = file_root / "fake.pdf"
        pdf_path.write_bytes(b"%PDF-1.4 fake content")

        monkeypatch.setitem(sys.modules, "pypdf", None)

        result = handlers["read_document"]({"path": str(pdf_path)})

        assert result == "Error: pypdf not installed"

    def test_docx_missing_dependency_returns_error_string(self, file_root, handlers, monkeypatch):
        docx_path = file_root / "fake.docx"
        docx_path.write_bytes(b"PK fake content")

        monkeypatch.setitem(sys.modules, "docx", None)

        result = handlers["read_document"]({"path": str(docx_path)})

        assert result == "Error: python-docx not installed"
