"""JSON-lines audit logger — records every tool call with verdict and outcome."""

import json
import os
from datetime import datetime, timezone


class AuditLogger:

    SENSITIVE_KEYS = {"password", "token", "secret", "api_key", "key", "credential"}

    def __init__(self, log_dir: str | None = None):
        if log_dir is None:
            log_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs")
        os.makedirs(log_dir, exist_ok=True)
        self._log_path = os.path.join(log_dir, "audit.jsonl")

    def log(
        self,
        tool_name: str,
        tool_input: dict,
        verdict: str,
        reason: str,
        result: str | None = None,
    ):
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "tool": tool_name,
            "input": self._redact(tool_input),
            "verdict": verdict,
            "reason": reason,
        }
        if result is not None:
            entry["result_preview"] = result[:200]
        with open(self._log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

    def _redact(self, obj: dict) -> dict:
        out = {}
        for k, v in obj.items():
            if k.lower() in self.SENSITIVE_KEYS:
                out[k] = "***REDACTED***"
            elif isinstance(v, dict):
                out[k] = self._redact(v)
            else:
                out[k] = v
        return out
