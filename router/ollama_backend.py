"""Ollama local LLM backend for free inference on simple requests."""

import logging

log = logging.getLogger(__name__)

OLLAMA_SYSTEM = (
    "You are JARVIS, a personal AI assistant. "
    "Answer briefly and conversationally. One or two sentences max."
)


class OllamaBackend:

    def __init__(
        self,
        model: str = "qwen2.5:3b-instruct-q4_K_M",
        base_url: str = "http://localhost:11434",
    ):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self._available: bool | None = None

    def is_available(self) -> bool:
        if self._available is not None:
            return self._available
        try:
            import requests
            r = requests.get(f"{self.base_url}/api/tags", timeout=2)
            self._available = r.status_code == 200
        except Exception:
            self._available = False
        if self._available:
            log.info("Ollama available at %s (model: %s)", self.base_url, self.model)
        else:
            log.info("Ollama not available — local tier will use canned responses")
        return self._available

    def generate(self, prompt: str) -> str:
        import requests
        r = requests.post(
            f"{self.base_url}/api/generate",
            json={
                "model": self.model,
                "prompt": prompt,
                "system": OLLAMA_SYSTEM,
                "stream": False,
                "options": {"temperature": 0.7, "num_predict": 256},
            },
            timeout=30,
        )
        r.raise_for_status()
        return r.json()["response"].strip()
