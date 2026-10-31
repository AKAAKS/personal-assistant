import pytest
import requests

from nutrition_core.adapters.http import RequestsTransport


class FakeResponse:
    def __init__(self, payload=None, error=None):
        self._payload = payload if payload is not None else {}
        self._error = error

    def raise_for_status(self):
        if self._error:
            raise self._error

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, response):
        self._response = response
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params, timeout))
        return self._response


class TestRequestsTransport:
    def test_get_json_passes_params_and_returns_parsed_json(self):
        session = FakeSession(FakeResponse({"foods": [1]}))
        transport = RequestsTransport(timeout=3.5, session=session)

        out = transport.get_json("http://x/y", {"q": "egg"})

        assert out == {"foods": [1]}
        assert session.calls == [("http://x/y", {"q": "egg"}, 3.5)]

    def test_get_json_raises_for_http_error(self):
        session = FakeSession(FakeResponse(error=requests.HTTPError("500")))
        transport = RequestsTransport(session=session)

        with pytest.raises(requests.HTTPError):
            transport.get_json("http://x/y")
