from __future__ import annotations

import requests


class RequestsTransport:
    """Real HTTP implementation of the Transport protocol.

    HTTP errors propagate via raise_for_status(); callers handle them.
    """

    def __init__(self, *, timeout: float = 10.0, session=None) -> None:
        self._timeout = timeout
        self._session = session

    def get_json(self, url: str, params: dict | None = None) -> dict:
        resp = (self._session or requests).get(url, params=params, timeout=self._timeout)
        resp.raise_for_status()
        return resp.json()
