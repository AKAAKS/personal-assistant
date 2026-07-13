"""File tools: search_files / read_document, allowlist-enforced.

The security guard (security/guard.py) is the enforcement backstop for every
tool call. The checks in this module exist for a friendlier UX (a clear
"Error: ..." string instead of a bare guard block) AND, for search_files, to
post-filter results: es.exe searches the *entire* disk, so every hit must be
re-validated against the allowlist and secret-file patterns before it is
ever returned to the model.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

from security.paths import allowed_roots, is_allowed_path, is_secret_path

MAX_RESULTS = 30
MAX_FILE_SIZE = 5 * 1024 * 1024  # 5 MB
RAW_RETURN_THRESHOLD = 6000
SUMMARIZER_INPUT_CAP = 30000
SKIP_DIR_NAMES = {".git", "node_modules", "__pycache__", ".venv"}

# run_script: whitelist-folder tool (Task 8). Scripts must live inside this
# directory (env override JARVIS_SCRIPTS_DIR, read per-call like
# security/paths.py does with FILE_ROOTS) -- nothing outside it can ever run.
SCRIPT_TIMEOUT_SEC = 60
OUTPUT_TAIL_CHARS = 2000
DEFAULT_SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent / "scripts" / "jarvis_approved"

TOOLS = [
    {
        "name": "search_files",
        "description": (
            "Search the user's files by filename (case-insensitive substring match). "
            "Only searches within allowed folders (Documents, Projects, Downloads). "
            "Returns up to 30 matching paths."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Filename substring to search for (e.g. 'invoice', 'report.pdf').",
                },
                "directory": {
                    "type": "string",
                    "description": (
                        "Optional directory to restrict the search to. Must be inside an "
                        "allowed folder. Optional — if omitted, searches all allowed folders."
                    ),
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "read_document",
        "description": (
            "Read the text content of a document (.txt, .md, .pdf, .docx, and other text "
            "files). Only files inside allowed folders (Documents, Projects, Downloads) can "
            "be read. Long documents are summarized instead of returned in full."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Full path to the document to read.",
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "run_script",
        "description": (
            "Run a script from the user's approved scripts folder (destructive -- "
            "requires confirmation). Supports .py, .bat, .cmd, and .ps1 files. "
            "If no name is given, or the name isn't found, lists the scripts "
            "available to run."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Filename of the script to run, e.g. 'backup.py'.",
                },
                "args": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional command-line arguments to pass to the script.",
                },
            },
            "required": [],
        },
    },
]


def handlers(summarizer=None):
    """File tool handlers.

    Args:
        summarizer: optional callable(text: str) -> str used to condense long
            documents. If None, long documents are truncated instead.

    Returns:
        dict: tool name -> callable(tool_input: dict) -> str
    """

    def search_files(ti):
        """Search allowed folders for files whose name matches query."""
        query = ti.get("query")
        directory = ti.get("directory")

        if not isinstance(query, str) or not query.strip():
            return "Error: query is required and must be a non-empty string."
        query = query.strip()

        if directory is not None:
            if not isinstance(directory, str) or not directory.strip():
                return "Error: directory must be a non-empty string."
            directory = directory.strip()
            try:
                resolved_dir = _resolve_directory(directory)
            except (OSError, ValueError):
                return "Error: directory could not be validated."
            # Dual check (canonical AND raw), same pattern as read_document and
            # the guard: canonical catches 8.3 short-name aliases like CREDEN~1.
            if is_secret_path(resolved_dir) or is_secret_path(directory):
                return "Error: access to secret-shaped files is not permitted."
            if not is_allowed_path(resolved_dir):
                return "Error: directory is outside the allowed folders (Documents, Projects, Downloads)."
            # Search the RESOLVED absolute path downstream, not the raw name — a
            # bare "Downloads" must not be re-resolved against the daemon's CWD.
            directory = str(resolved_dir)

        if shutil.which("es"):
            hits = _search_es(query, directory)
        else:
            hits = _search_fallback(query, directory)

        if not hits:
            return f"No files found matching '{query}'."
        return "\n".join(hits)

    def read_document(ti):
        """Read a document's text content, summarizing if it is long."""
        path = ti.get("path")
        if not isinstance(path, str) or not path.strip():
            return "Error: path is required and must be a non-empty string."
        path = path.strip()

        try:
            resolved = Path(path).expanduser().resolve()
        except (OSError, ValueError):
            return "Error: invalid path."

        if is_secret_path(resolved) or is_secret_path(path):
            return "Error: access to secret-shaped files is not permitted."
        if not is_allowed_path(resolved):
            return "Error: path is outside the allowed folders (Documents, Projects, Downloads)."

        if not resolved.exists() or not resolved.is_file():
            return f"Error: file not found: {path}"

        try:
            size = resolved.stat().st_size
        except OSError as exc:
            return f"Error: {exc}"
        if size > MAX_FILE_SIZE:
            return "Error: file too large"

        ext = resolved.suffix.lower()
        try:
            if ext == ".pdf":
                try:
                    from pypdf import PdfReader
                except ImportError:
                    return "Error: pypdf not installed"
                reader = PdfReader(str(resolved))
                text = "\n".join(page.extract_text() or "" for page in reader.pages)
            elif ext == ".docx":
                try:
                    from docx import Document
                except ImportError:
                    return "Error: python-docx not installed"
                doc = Document(str(resolved))
                text = "\n".join(paragraph.text for paragraph in doc.paragraphs)
            else:
                text = resolved.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            return f"Error: {exc}"

        if len(text) <= RAW_RETURN_THRESHOLD:
            return text

        if summarizer is not None:
            try:
                summary = summarizer(text[:SUMMARIZER_INPUT_CAP])
            except Exception as exc:
                return f"Error: {exc}"
            return f"Summary of {resolved.name}:\n{summary}"

        return text[:RAW_RETURN_THRESHOLD] + "\n...[truncated]"

    def run_script(ti):
        """Run a whitelisted script (name resolved against the approved
        scripts folder) and return its exit code + truncated stdout/stderr."""
        name = ti.get("name")
        raw_args = ti.get("args")

        if raw_args is None:
            args = []
        elif not isinstance(raw_args, list) or not all(isinstance(a, str) for a in raw_args):
            return "Error: args must be an array of strings."
        else:
            args = raw_args

        scripts_dir = _scripts_dir()

        if not isinstance(name, str) or not name.strip():
            return _list_available_scripts(scripts_dir)
        name = name.strip()

        # Reject path separators/traversal BEFORE any resolution -- defense
        # in depth on top of the is_relative_to check below.
        if "/" in name or "\\" in name or ".." in name:
            return "Error: invalid script name."

        try:
            resolved_dir = scripts_dir.resolve()
        except (OSError, ValueError):
            return _list_available_scripts(scripts_dir)

        try:
            resolved = (scripts_dir / name).resolve()
        except (OSError, ValueError):
            return "Error: invalid script name."

        if not resolved.is_relative_to(resolved_dir):
            return "Error: invalid script name."

        if not resolved.exists() or not resolved.is_file():
            return _list_available_scripts(scripts_dir)

        ext = resolved.suffix.lower()
        if ext == ".py":
            cmd = [sys.executable, str(resolved), *args]
        elif ext in (".bat", ".cmd"):
            cmd = ["cmd", "/c", str(resolved), *args]
        elif ext == ".ps1":
            cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(resolved), *args]
        else:
            return "Error: unsupported script type"

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=SCRIPT_TIMEOUT_SEC,
            )
        except subprocess.TimeoutExpired:
            return f"Error: script timed out after {SCRIPT_TIMEOUT_SEC}s"
        except OSError as exc:
            return f"Error: {exc}"

        stdout_tail = (result.stdout or "")[-OUTPUT_TAIL_CHARS:]
        stderr_tail = (result.stderr or "")[-OUTPUT_TAIL_CHARS:]
        return (
            f"Exit code: {result.returncode}\n"
            f"--- stdout (last {OUTPUT_TAIL_CHARS} chars) ---\n{stdout_tail}\n"
            f"--- stderr (last {OUTPUT_TAIL_CHARS} chars) ---\n{stderr_tail}"
        )

    return {
        "search_files": search_files,
        "read_document": read_document,
        "run_script": run_script,
    }


def _scripts_dir() -> Path:
    """Whitelist directory for run_script. Read the env override on every
    call (no caching -- tests monkeypatch it and expect the change to take
    effect), same pattern as security/paths.py's allowed_roots()."""
    raw = os.environ.get("JARVIS_SCRIPTS_DIR")
    if raw:
        return Path(raw).expanduser()
    return DEFAULT_SCRIPTS_DIR


def _list_available_scripts(scripts_dir: Path) -> str:
    if not scripts_dir.exists() or not scripts_dir.is_dir():
        return "No scripts are available."
    names = sorted(p.name for p in scripts_dir.iterdir() if p.is_file())
    if not names:
        return "No scripts are available."
    return "Available scripts: " + ", ".join(names)


def _resolve_directory(directory: str) -> Path:
    """Resolve a user/model-supplied search `directory` sensibly.

    A bare or relative name like "Downloads" must resolve against the ALLOWED
    ROOTS (the user's Documents/Projects/Downloads), NOT the daemon's working
    directory — otherwise "Downloads" silently becomes <cwd>/Downloads, which
    doesn't exist, and the search returns nothing. Absolute paths are honored
    as given; a bare name that matches no allowed root falls through to a normal
    resolve so is_allowed_path can reject it with a clear message.
    """
    p = Path(directory).expanduser()
    if p.is_absolute():
        return p.resolve()
    needle = directory.replace("\\", "/").strip("/").lower()
    for root in allowed_roots():
        if root.name.lower() == needle:
            return root
        candidate = (root / directory).resolve()
        if candidate.exists():
            return candidate
    return p.resolve()


def _search_es(query: str, directory: "str | None") -> list[str]:
    """Search via Everything's es.exe CLI (indexes the whole disk -- results
    below are always post-filtered against the allowlist)."""
    cmd = ["es", "-n", str(MAX_RESULTS)]
    if directory:
        cmd += ["-path", directory]
    cmd.append(query)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return _search_fallback(query, directory)

    hits = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        if is_secret_path(line) or not is_allowed_path(line):
            continue
        hits.append(line)
        if len(hits) >= MAX_RESULTS:
            break
    return hits


def _search_fallback(query: str, directory: "str | None") -> list[str]:
    """os.walk fallback when es.exe is unavailable."""
    query_lower = query.lower()

    if directory:
        try:
            roots = [Path(directory).expanduser().resolve()]
        except (OSError, ValueError):
            return []
    else:
        roots = allowed_roots()

    hits: list[str] = []
    for root in roots:
        if len(hits) >= MAX_RESULTS:
            break
        if not root.exists():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIR_NAMES]
            for name in filenames:
                if query_lower not in name.lower():
                    continue
                full = Path(dirpath) / name
                if is_secret_path(full) or not is_allowed_path(full):
                    continue
                hits.append(str(full))
                if len(hits) >= MAX_RESULTS:
                    break
            if len(hits) >= MAX_RESULTS:
                break
    return hits
