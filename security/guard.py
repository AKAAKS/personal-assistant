"""Security guard — validates every tool call against the security policy."""

import hashlib
import json
from dataclasses import dataclass
from enum import Enum

from security.policy import (
    BLOCKED_KEYWORDS,
    BLOCKED_URL_PATTERNS,
    DEFAULT_PERMISSION,
    TOOL_PERMISSIONS,
)


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

    def check(self, tool_name: str, tool_input: dict) -> SecurityResult:
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

        permission = TOOL_PERMISSIONS.get(tool_name, DEFAULT_PERMISSION)

        if permission == "forbidden":
            return SecurityResult(
                Verdict.BLOCK,
                f"Blocked: tool '{tool_name}' is forbidden by security policy.",
            )

        if permission == "destructive":
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
