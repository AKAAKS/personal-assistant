"""Filesystem allowlist for file tools. Roots come from FILE_ROOTS env
(semicolon-separated) or default to ~/Documents;~/Projects;~/Downloads."""
import os
from fnmatch import fnmatch
from pathlib import Path

DEFAULT_ROOTS = ["~/Documents", "~/Projects", "~/Downloads"]

SECRET_FILE_PATTERNS = [  # matched against the basename, lowercased
    ".env", "*.pem", "*.key", "*.pfx", "*.p12", "id_rsa*", "id_ed25519*",
    "*credential*", "*secret*", "*token*.json", ".netrc", ".npmrc", ".pypirc",
]


def allowed_roots() -> list[Path]:
    """Read FILE_ROOTS from the environment on every call (no caching --
    tests monkeypatch this env var and expect the change to take effect)."""
    raw = os.environ.get("FILE_ROOTS")
    parts = raw.split(";") if raw else DEFAULT_ROOTS
    roots = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        try:
            roots.append(Path(part).expanduser().resolve())
        except (OSError, ValueError):
            continue
    return roots


def is_allowed_path(path: str) -> bool:
    try:
        resolved = Path(path).expanduser().resolve()
    except (OSError, ValueError):
        return False
    for root in allowed_roots():
        if resolved.is_relative_to(root):
            return True
    return False


def is_secret_path(path: str) -> bool:
    p = Path(path)
    if any(part == ".env" for part in p.parts):
        return True
    name = p.name.lower()
    return any(fnmatch(name, pat) for pat in SECRET_FILE_PATTERNS)
