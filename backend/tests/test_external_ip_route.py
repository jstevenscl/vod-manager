"""External-IP diagnostic route uses httpx like the rest of the app
(upstream PR #35 review) and caches its answer."""

import asyncio

import routes


class _FakeResponse:
    def raise_for_status(self):
        pass

    def json(self):
        return {"ip": "203.0.113.7"}


def test_external_ip_fetches_via_httpx_and_caches(monkeypatch):
    calls = []

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, **kwargs):
            calls.append(url)
            return _FakeResponse()

    monkeypatch.setattr(routes.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(routes, "_external_ip_cache", (None, float("-inf")))

    first = asyncio.run(routes.get_external_ip())
    second = asyncio.run(routes.get_external_ip())

    assert first == second == {"ip": "203.0.113.7", "available": True}
    assert len(calls) == 1
