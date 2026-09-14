"""Plan-doc follow-up (2026-09-14): "provider HTTP-client lifecycle". Pooled
per-provider httpx.AsyncClients (vod_importer._PROVIDER_CLIENTS) correctly
live for the process lifetime to avoid per-title connection churn, but were
never explicitly closed at application shutdown, nor when a provider is
deleted, nor when its connection settings (base_url/username/password/
custom_user_agent) change. A stale pooled client kept using old credentials/
headers against a provider that no longer exists (or whose URL/auth just
changed) until the next backoff trip happened to evict it -- which might
never happen for a provider that was simply deleted.

Fix: two explicit cleanup entry points in vod_importer.py --
  - evict_provider_client(provider_id): closes (queues via
    _CLIENTS_PENDING_CLOSE, same drain path backoff eviction already uses)
    and removes that ONE provider's pooled client, plus its limiter/backoff
    state, so the next call (if any) builds a genuinely fresh client under
    current credentials/headers.
  - close_all_provider_clients(): evicts every pooled client -- called from
    main.py's lifespan shutdown so no sockets/TLS sessions outlive the
    process.
vod_routes.py's delete_provider, and every connection-setting route
(base-url, user-agent, and upsert_provider's username/password update path),
now call evict_provider_client after the DB write succeeds."""

import asyncio

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


def test_evict_provider_client_closes_and_removes_the_pooled_client():
    provider_id = 111
    client = vod_importer._get_provider_client(provider_id, {})
    assert not client.is_closed

    asyncio.run(vod_importer.evict_provider_client(provider_id))

    assert provider_id not in vod_importer._PROVIDER_CLIENTS
    assert client.is_closed


def test_evict_provider_client_also_clears_limiter_and_backoff_state():
    provider_id = 222
    vod_importer._get_provider_client(provider_id, {})
    vod_importer._get_provider_limiter(provider_id)
    vod_importer._record_provider_failure(provider_id, "Test Provider")
    assert provider_id in vod_importer._PROVIDER_LIMITERS

    asyncio.run(vod_importer.evict_provider_client(provider_id))

    assert provider_id not in vod_importer._PROVIDER_LIMITERS
    assert provider_id not in vod_importer._PROVIDER_BACKOFF


def test_evict_provider_client_is_a_noop_for_a_provider_with_no_pooled_client():
    """Deleting/editing a provider that was never actually used (imported/
    enriched) yet -- no pooled client exists -- must not raise."""
    asyncio.run(vod_importer.evict_provider_client(999999))


def test_close_all_provider_clients_closes_every_pooled_client():
    ids = [301, 302, 303]
    clients = [vod_importer._get_provider_client(pid, {}) for pid in ids]
    assert all(not c.is_closed for c in clients)

    asyncio.run(vod_importer.close_all_provider_clients())

    assert vod_importer._PROVIDER_CLIENTS == {}
    assert all(c.is_closed for c in clients)


def test_close_all_provider_clients_is_a_noop_when_none_are_pooled():
    asyncio.run(vod_importer.close_all_provider_clients())
    assert vod_importer._PROVIDER_CLIENTS == {}
