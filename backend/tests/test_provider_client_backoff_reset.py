"""beads-j6q follow-up: WOBO showed ~11K movie backoff-skips in a single bulk
enrich run (3-consecutive-failures -> 15s backoff -> recovered, repeating).
_get_provider_client's persistent httpx.AsyncClient (vod_importer.py) was never
torn down across a backoff cycle -- same connection pool, same TLS session
identity resumes immediately once the 15s cooldown clears, which looks
identical to WOBO as the traffic it just rate-limited. User's fix: evict (and
close) the pooled client the moment a provider's backoff actually triggers, so
the next call after cooldown opens a genuinely fresh connection instead of
reusing the one WOBO just flagged.

Also: _AdaptiveLimiter.note_failure() already halves the logical concurrency
cap (8->4->2->1), but the physical pool was always rebuilt at the hardcoded
_PROVIDER_MAX_CONCURRENCY (8) regardless of that cap -- so the fresh client
built after backoff should be sized to the limiter's CURRENT cap, not always 8,
otherwise the physical pool still allows more concurrent sockets than the
logical limiter is actually granting."""

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


def test_client_persists_across_failures_below_threshold():
    """Below the backoff threshold, the same pooled client should still be
    reused -- only an actual backoff trip should force a new one."""
    provider_id = 101
    client_before = vod_importer._get_provider_client(provider_id, {})

    for _ in range(vod_importer._BACKOFF_FAILURE_THRESHOLD - 1):
        vod_importer._record_provider_failure(provider_id, "Test Provider")

    client_after = vod_importer._get_provider_client(provider_id, {})
    assert client_after is client_before


def test_backoff_trip_evicts_pooled_client():
    """Once failures cross the threshold and backoff actually triggers, the
    stale client must be evicted so the next call opens a fresh connection
    rather than resuming through the same pool/TLS session WOBO just
    rate-limited."""
    provider_id = 202
    client_before = vod_importer._get_provider_client(provider_id, {})

    for _ in range(vod_importer._BACKOFF_FAILURE_THRESHOLD):
        vod_importer._record_provider_failure(provider_id, "Test Provider")

    assert provider_id not in vod_importer._PROVIDER_CLIENTS

    client_after = vod_importer._get_provider_client(provider_id, {})
    assert client_after is not client_before


def test_evicted_client_is_closed():
    """The old client shouldn't just be dropped from the dict -- its
    connections need to actually be closed, or the process leaks sockets."""
    provider_id = 303
    client_before = vod_importer._get_provider_client(provider_id, {})
    assert not client_before.is_closed

    for _ in range(vod_importer._BACKOFF_FAILURE_THRESHOLD):
        vod_importer._record_provider_failure(provider_id, "Test Provider")

    asyncio.run(vod_importer._drain_closed_clients())
    assert client_before.is_closed


def test_repeated_backoff_trips_keep_evicting():
    """A provider that keeps failing through multiple cooldown tiers (the
    exponential-backoff path) should get a fresh client every time it trips
    again, not just the first time."""
    provider_id = 404
    seen_clients = []

    for _ in range(3):
        seen_clients.append(vod_importer._get_provider_client(provider_id, {}))
        for _ in range(vod_importer._BACKOFF_FAILURE_THRESHOLD):
            vod_importer._record_provider_failure(provider_id, "Test Provider")

    asyncio.run(vod_importer._drain_closed_clients())
    assert len(set(id(c) for c in seen_clients)) == 3
    assert all(c.is_closed for c in seen_clients)


def test_fresh_client_pool_sized_to_current_limiter_cap():
    """The rebuilt client's connection pool should reflect the limiter's
    narrowed cap (e.g. 4 after one halving from 8), not always the hardcoded
    max -- otherwise the physical pool still permits more concurrent sockets
    than the logical limiter is granting."""
    provider_id = 505
    vod_importer._get_provider_client(provider_id, {})
    limiter = vod_importer._get_provider_limiter(provider_id)

    asyncio.run(limiter.note_failure())  # 8 -> 4
    assert limiter.cap == 4

    for _ in range(vod_importer._BACKOFF_FAILURE_THRESHOLD):
        vod_importer._record_provider_failure(provider_id, "Test Provider")

    fresh_client = vod_importer._get_provider_client(provider_id, {})
    pool_limits = fresh_client._transport._pool._max_connections
    assert pool_limits == 4


def test_success_does_not_evict_client():
    """A clean call clearing a provider's backoff state should NOT itself
    force a client rebuild -- only an actual triggered backoff should."""
    provider_id = 606
    client_before = vod_importer._get_provider_client(provider_id, {})

    vod_importer._record_provider_failure(provider_id, "Test Provider")  # below threshold
    vod_importer._record_provider_success(provider_id)

    client_after = vod_importer._get_provider_client(provider_id, {})
    assert client_after is client_before
