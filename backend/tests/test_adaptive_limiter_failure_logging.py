"""Regression coverage for adaptive-limiter failure logging.

_AdaptiveLimiter.note_failure() halves a provider's concurrency cap on a
qualifying failure/timeout independently of the binary _PROVIDER_BACKOFF
threshold. The log must explain a visible cap reduction with a provider label
and a concrete reason.

Fix: note_failure() takes an optional `reason` string and logs a warning
naming the provider and the reason whenever it actually halves the cap, and
both call sites in XCProviderClient._call() (the httpx exception branch and
the HTTPStatusError branch) now pass a reason describing the actual trigger
(exception type name, or HTTP status code) instead of calling note_failure()
blind."""

import asyncio
import logging

import pytest

import vod_importer


@pytest.fixture(autouse=True)
def _reset_provider_state():
    vod_importer._PROVIDER_BACKOFF.clear()
    vod_importer._PROVIDER_CLIENTS.clear()
    vod_importer._PROVIDER_LIMITERS.clear()
    yield
    vod_importer._PROVIDER_BACKOFF.clear()
    vod_importer._PROVIDER_CLIENTS.clear()
    vod_importer._PROVIDER_LIMITERS.clear()


def test_note_failure_logs_reason_and_new_cap(caplog):
    limiter = vod_importer._AdaptiveLimiter()
    with caplog.at_level(logging.WARNING, logger="vod_importer"):
        asyncio.run(limiter.note_failure(provider_name="Example Provider", reason="HTTP 429"))

    assert limiter.cap == 4
    messages = [r.message for r in caplog.records]
    assert any("Example Provider" in m and "HTTP 429" in m and "4" in m for m in messages), messages


def test_note_failure_without_reason_still_logs_something(caplog):
    """Callers that can't identify a reason should still get a log line
    naming the provider and new cap, not silence."""
    limiter = vod_importer._AdaptiveLimiter()
    with caplog.at_level(logging.WARNING, logger="vod_importer"):
        asyncio.run(limiter.note_failure(provider_name="Some Provider"))

    messages = [r.message for r in caplog.records]
    assert any("Some Provider" in m for m in messages), messages


def test_call_site_passes_exception_type_as_reason(monkeypatch):
    """The httpx exception branch in XCProviderClient._call should pass the
    concrete exception type name as the reason, not call note_failure() with
    no context."""
    import httpx

    captured = {}

    class _FakeLimiter:
        async def acquire(self):
            pass

        async def release(self):
            pass

        async def note_failure(self, **kwargs):
            captured.update(kwargs)

        async def note_success(self):
            pass

    provider = {"id": 707, "name": "Test Provider", "base_url": "http://example.test",
                "username": "u", "password": "p"}
    client = vod_importer.XCProviderClient(provider)

    monkeypatch.setattr(vod_importer, "_get_provider_limiter", lambda pid: _FakeLimiter())
    monkeypatch.setattr(vod_importer, "_provider_backoff_remaining", lambda pid: 0)
    monkeypatch.setattr(vod_importer, "_record_provider_failure", lambda pid, name: None)

    class _FakeClient:
        async def get(self, *a, **kw):
            raise httpx.ConnectTimeout("boom")

    monkeypatch.setattr(vod_importer, "_get_provider_client", lambda pid, headers: _FakeClient())

    with pytest.raises(httpx.ConnectTimeout):
        asyncio.run(client._call())

    assert "reason" in captured
    assert "ConnectTimeout" in captured["reason"]


def test_call_site_passes_status_code_as_reason(monkeypatch):
    import httpx

    captured = {}

    class _FakeLimiter:
        async def acquire(self):
            pass

        async def release(self):
            pass

        async def note_failure(self, **kwargs):
            captured.update(kwargs)

        async def note_success(self):
            pass

    provider = {"id": 808, "name": "Test Provider", "base_url": "http://example.test",
                "username": "u", "password": "p"}
    client = vod_importer.XCProviderClient(provider)

    monkeypatch.setattr(vod_importer, "_get_provider_limiter", lambda pid: _FakeLimiter())
    monkeypatch.setattr(vod_importer, "_provider_backoff_remaining", lambda pid: 0)
    monkeypatch.setattr(vod_importer, "_record_provider_failure", lambda pid, name: None)

    request = httpx.Request("GET", "http://example.test/player_api.php")
    response = httpx.Response(429, request=request)

    class _FakeClient:
        async def get(self, *a, **kw):
            return response

    monkeypatch.setattr(vod_importer, "_get_provider_client", lambda pid, headers: _FakeClient())

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(client._call())

    assert captured.get("reason") == "HTTP 429"
