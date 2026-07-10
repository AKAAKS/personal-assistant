"""Security guard — validates every tool call against the security policy."""

import hashlib
import json
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from security.paths import is_allowed_path, is_secret_path
from security.policy import (
    BLOCKED_KEYWORDS,
    BLOCKED_URL_PATTERNS,
    DEFAULT_PERMISSION,
    PATH_CHECKED_TOOLS,
    TOOL_PERMISSIONS,
)

_PATH_SEP_RE = re.compile(r"[\\/]")


class Verdict(Enum):
    ALLOW = "allow"
    BLOCK = "block"
    CONFIRM_REQUIRED = "confirm_required"


@dataclass
class SecurityResult:
    verdict: Verdict
    reason: str


class SecurityGuard:

    def __init__(self):
        self._pending_confirmations: set[str] = set()

    def check(self, tool_name: str, tool_input: dict, autonomous: bool = False) -> SecurityResult:
        """Validate one tool call.

        autonomous=True marks calls with no user present (trigger rules,
        reminders): the retry-based confirm dance below is meaningless there,
        so destructive verdicts become terminal CONFIRM_REQUIRED and never
        touch the pending-confirmation state.
        """
        blocked_kw = self._check_blocked_keywords(tool_input)
        if blocked_kw:
            return SecurityResult(
                Verdict.BLOCK,
                f"Blocked: input contains sensitive keyword '{blocked_kw}'. "
                "Access to financial data and sensitive IDs is not permitted.",
            )

        blocked_url = self._check_blocked_urls(tool_input)
        if blocked_url:
            return SecurityResult(
                Verdict.BLOCK,
                f"Blocked: URL matches restricted pattern '{blocked_url}'. "
                "Access to banking and financial sites is not permitted.",
            )

        path_verdict = self._check_paths(tool_name, tool_input)
        if path_verdict:
            return path_verdict

        permission = TOOL_PERMISSIONS.get(tool_name, DEFAULT_PERMISSION)

        if permission == "forbidden":
            return SecurityResult(
                Verdict.BLOCK,
                f"Blocked: tool '{tool_name}' is forbidden by security policy.",
            )

        if permission == "destructive":
            if autonomous:
                # No user is present to confirm. Do NOT arm _pending_confirmations
                # (an autonomous retry with identical input would auto-confirm) and
                # do NOT consume a confirmation a real user may have pending for
                # the same action.
                return SecurityResult(
                    Verdict.CONFIRM_REQUIRED,
                    f"Tool '{tool_name}' is destructive and cannot run autonomously; "
                    "the user must initiate and confirm this action themselves.",
                )
            key = self._action_key(tool_name, tool_input)
            if key in self._pending_confirmations:
                self._pending_confirmations.discard(key)
                return SecurityResult(Verdict.ALLOW, "Confirmed by user.")
            self._pending_confirmations.add(key)
            return SecurityResult(
                Verdict.CONFIRM_REQUIRED,
                f"Tool '{tool_name}' is destructive and requires user confirmation. "
                "Please ask the user to confirm before retrying.",
            )

        return SecurityResult(Verdict.ALLOW, f"Allowed ({permission}).")

    def clear_pending(self):
        self._pending_confirmations.clear()

    # -- internals --

    @staticmethod
    def _action_key(tool_name: str, tool_input: dict) -> str:
        raw = f"{tool_name}:{json.dumps(tool_input, sort_keys=True)}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    @staticmethod
    def _extract_strings(obj) -> list[str]:
        if isinstance(obj, str):
            return [obj]
        if isinstance(obj, dict):
            return [s for v in obj.values() for s in SecurityGuard._extract_strings(v)]
        if isinstance(obj, list):
            return [s for item in obj for s in SecurityGuard._extract_strings(item)]
        return []

    @staticmethod
    def _check_blocked_keywords(tool_input: dict) -> str | None:
        combined = " ".join(SecurityGuard._extract_strings(tool_input)).lower()
        for kw in BLOCKED_KEYWORDS:
            if kw in combined:
                return kw
        return None

    @staticmethod
    def _check_blocked_urls(tool_input: dict) -> str | None:
        for key, value in tool_input.items():
            if not isinstance(value, str):
                continue
            if key.lower() in ("url", "uri", "link", "href") or value.startswith("http"):
                val_lower = value.lower()
                for pattern in BLOCKED_URL_PATTERNS:
                    if pattern in val_lower:
                        return pattern
        return None

    @staticmethod
    def _check_paths(tool_name: str, tool_input: dict) -> "SecurityResult | None":
        # Tool-specific: validate the configured path-bearing keys for this tool.
        for key in PATH_CHECKED_TOOLS.get(tool_name, []):
            value = tool_input.get(key)
            if not isinstance(value, str) or not value:
                continue
            try:
                canonical = Path(value).expanduser().resolve()
            except (OSError, ValueError):
                return SecurityResult(
                    Verdict.BLOCK, "Blocked: path could not be validated."
                )
            # Check BOTH spellings: the canonical path (expands 8.3 short
            # names like ENV~1 and strips Windows trailing dots/spaces) and
            # the raw value (is_secret_path strips trailing dots/spaces per
            # component itself, covering paths that don't exist on disk).
            if is_secret_path(canonical) or is_secret_path(value):
                return SecurityResult(
                    Verdict.BLOCK,
                    "Blocked: access to secret-shaped files is not permitted.",
                )
            if not is_allowed_path(canonical):
                return SecurityResult(
                    Verdict.BLOCK,
                    "Blocked: path is outside the allowed folders (Documents, Projects, Downloads).",
                )

        # Everywhere: hard block on secret-shaped files in ANY tool's input,
        # regardless of whether that tool is in PATH_CHECKED_TOOLS.
        for s in SecurityGuard._extract_strings(tool_input):
            if _PATH_SEP_RE.search(s) and is_secret_path(s):
                return SecurityResult(
                    Verdict.BLOCK,
                    "Blocked: access to secret-shaped files is not permitted.",
                )
        return None
