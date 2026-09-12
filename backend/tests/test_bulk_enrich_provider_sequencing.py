"""beads-f7e / beads-sw9 redesign (2026-09-12): bulk_enrich_all used to run
EVERY movie and EVERY series across ALL providers concurrently in one flat
asyncio.gather(), each kind sharing one global semaphore. That let movie and
series failures against the SAME provider pile onto that provider's shared
3-strike backoff threshold (_BACKOFF_FAILURE_THRESHOLD) at once, tripping
backoff faster than the old (2026-08-20-replaced) strictly-sequential
"movies-then-series" design ever did -- confirmed live on WOBO and WarpTV
simultaneously during a mass re-enrichment catch-up triggered by the
episodes_last_enriched_at NULL-backfill (beads-sw9).

Approved redesign (see beads-sw9 comment, authoritative spec):
  1. Per-provider sequencing: each provider's own movies enrich, then that
     SAME provider's own series enrich after -- but provider A's series can
     run while provider B is still on movies (no GLOBAL movies-then-series
     gate, no cross-provider blocking).
  2. Per-provider failure isolation, exactly one retry:
     - Provider X's movie phase stalls/fails -> skip X entirely (no series
       either) while every OTHER provider continues normally.
     - After every OTHER provider has finished both phases, retry X's movies
       ONCE. Retry succeeds -> proceed to X's series normally. Retry fails
       again -> flag X's movie enrichment incomplete, do NOT attempt X's
       series at all, no third attempt.
     - Provider X's SERIES phase (after movies already succeeded) stalls ->
       no retry, no re-running movies, just flag X's series incomplete.
  3. Auto-merge moves from inline-per-item to two end-of-phase sweeps: one
     movie merge pass after ALL providers have resolved their movie phase,
     one series merge pass after ALL providers have resolved their series
     phase. "Resolved" includes both success and failed-after-retry.

These tests exercise vod_importer.bulk_enrich_all's ORCHESTRATION only --
enrich_movie/enrich_series/vod_db/auto-merge are all mocked out, since the
sequencing/retry/merge-timing rules above are pure scheduling logic
independent of what any single item's real enrichment does."""

import asyncio

import pytest

import vod_importer


@pytest.fixture(autouse=True)
def _reset_enrich_progress():
    vod_importer._ENRICH_PROGRESS.update({
        "running": False,
        "movies_total": 0, "movies_done": 0, "movies_errors": 0, "movies_backoff_skipped": 0,
        "series_total": 0, "series_done": 0, "series_errors": 0, "series_backoff_skipped": 0,
        "started_at": None, "finished_at": None,
        "providers_incomplete": [],
    })
    yield


def _provider(pid, name):
    return {"id": pid, "name": name, "provider_type": "xc"}


def test_each_providers_series_wait_for_that_same_providers_movies(monkeypatch):
    """Provider A's series enrichment must not start before Provider A's own
    movies finish -- the core per-provider sequencing rule."""
    events = []

    async def fake_enrich_movie(movie_id, *, force=False, skip_auto_merge=False):
        await asyncio.sleep(0.01)
        events.append(("movie", movie_id))
        return True

    async def fake_enrich_series(series_id, *, force=False, skip_auto_merge=False):
        events.append(("series", series_id))
        return {"fetched": True, "reason": None}

    monkeypatch.setattr(vod_importer, "enrich_movie", fake_enrich_movie)
    monkeypatch.setattr(vod_importer, "enrich_series", fake_enrich_series)
    monkeypatch.setattr(vod_importer.vod_db, "list_providers", lambda: [_provider(1, "ProvA")])
    monkeypatch.setattr(vod_importer.vod_db, "list_all_movie_ids", lambda **kw: [10])
    monkeypatch.setattr(vod_importer.vod_db, "list_all_series_ids", lambda **kw: [20])
    monkeypatch.setattr(vod_importer.vod_db, "auto_merge_movie_by_tmdb", lambda movie_id: None)
    monkeypatch.setattr(vod_importer.vod_db, "auto_merge_series_by_tmdb", lambda series_id: None)

    asyncio.run(vod_importer.bulk_enrich_all(concurrency=8))

    movie_index = events.index(("movie", 10))
    series_index = events.index(("series", 20))
    assert movie_index < series_index


def test_one_providers_movies_do_not_block_another_providers_series(monkeypatch):
    """Provider B should be able to reach its series phase while Provider A
    is still working through a slow movie phase -- no GLOBAL movies-then-
    series gate."""
    events = []
    provider_a_movie_started = asyncio.Event()
    provider_b_series_done = asyncio.Event()

    async def fake_enrich_movie(movie_id, *, force=False, skip_auto_merge=False):
        if movie_id == 10:  # Provider A's movie -- deliberately slow
            provider_a_movie_started.set()
            await provider_b_series_done.wait()
        events.append(("movie", movie_id))
        return True

    async def fake_enrich_series(series_id, *, force=False, skip_auto_merge=False):
        events.append(("series", series_id))
        if series_id == 21:  # Provider B's series
            provider_b_series_done.set()
        return {"fetched": True, "reason": None}

    monkeypatch.setattr(vod_importer, "enrich_movie", fake_enrich_movie)
    monkeypatch.setattr(vod_importer, "enrich_series", fake_enrich_series)
    monkeypatch.setattr(vod_importer.vod_db, "list_providers", lambda: [_provider(1, "ProvA"), _provider(2, "ProvB")])
    monkeypatch.setattr(
        vod_importer.vod_db, "list_all_movie_ids",
        lambda provider_id=None, **kw: [10] if provider_id == 1 else [11],
    )
    monkeypatch.setattr(
        vod_importer.vod_db, "list_all_series_ids",
        lambda provider_id=None, **kw: [20] if provider_id == 1 else [21],
    )
    monkeypatch.setattr(vod_importer.vod_db, "auto_merge_movie_by_tmdb", lambda movie_id: None)
    monkeypatch.setattr(vod_importer.vod_db, "auto_merge_series_by_tmdb", lambda series_id: None)

    asyncio.run(asyncio.wait_for(vod_importer.bulk_enrich_all(concurrency=8), timeout=5))

    # Provider B's series finished (unblocking A) BEFORE Provider A's movie
    # itself finished -- proof A's slow movie phase never blocked B.
    assert ("series", 21) in events
    assert ("movie", 10) in events
    assert events.index(("series", 21)) < events.index(("movie", 10))


def test_provider_whose_movies_fail_is_skipped_while_others_continue(monkeypatch):
    """Provider X's movie phase raising should NOT block/delay Provider Y at
    all -- Y must run its full movies+series normally while X is set aside
    for the later single retry."""
    events = []

    async def fake_enrich_movie(movie_id, *, force=False, skip_auto_merge=False):
        if movie_id == 10:  # Provider X (id=1) -- always fails
            raise RuntimeError("provider X movie enrichment stalled")
        events.append(("movie", movie_id))
        return True

    async def fake_enrich_series(series_id, *, force=False, skip_auto_merge=False):
        events.append(("series", series_id))
        return {"fetched": True, "reason": None}

    monkeypatch.setattr(vod_importer, "enrich_movie", fake_enrich_movie)
    monkeypatch.setattr(vod_importer, "enrich_series", fake_enrich_series)
    monkeypatch.setattr(vod_importer.vod_db, "list_providers", lambda: [_provider(1, "ProvX"), _provider(2, "ProvY")])
    monkeypatch.setattr(
        vod_importer.vod_db, "list_all_movie_ids",
        lambda provider_id=None, **kw: [10] if provider_id == 1 else [11],
    )
    monkeypatch.setattr(
        vod_importer.vod_db, "list_all_series_ids",
        lambda provider_id=None, **kw: [20] if provider_id == 1 else [21],
    )
    monkeypatch.setattr(vod_importer.vod_db, "auto_merge_movie_by_tmdb", lambda movie_id: None)
    monkeypatch.setattr(vod_importer.vod_db, "auto_merge_series_by_tmdb", lambda series_id: None)

    asyncio.run(asyncio.wait_for(vod_importer.bulk_enrich_all(concurrency=8), timeout=5))

    # Provider Y ran its full movies+series completely unaffected.
    assert ("movie", 11) in events
    assert ("series", 21) in events
    # Provider X's series (20) must never run -- its movies never succeeded
    # even after the retry.
    assert ("series", 20) not in events


def test_provider_movie_retry_succeeds_then_series_runs(monkeypatch):
    """Provider X's movie phase fails once, then -- after every other
    provider has finished -- gets retried exactly once. If that retry
    succeeds, X's series must proceed normally."""
    call_count = {"movie10": 0}
    events = []

    async def fake_enrich_movie(movie_id, *, force=False, skip_auto_merge=False):
        if movie_id == 10:
            call_count["movie10"] += 1
            if call_count["movie10"] == 1:
                raise RuntimeError("transient failure")
        events.append(("movie", movie_id))
        return True

    async def fake_enrich_series(series_id, *, force=False, skip_auto_merge=False):
        events.append(("series", series_id))
        return {"fetched": True, "reason": None}

    monkeypatch.setattr(vod_importer, "enrich_movie", fake_enrich_movie)
    monkeypatch.setattr(vod_importer, "enrich_series", fake_enrich_series)
    monkeypatch.setattr(vod_importer.vod_db, "list_providers", lambda: [_provider(1, "ProvX"), _provider(2, "ProvY")])
    monkeypatch.setattr(
        vod_importer.vod_db, "list_all_movie_ids",
        lambda provider_id=None, **kw: [10] if provider_id == 1 else [11],
    )
    monkeypatch.setattr(
        vod_importer.vod_db, "list_all_series_ids",
        lambda provider_id=None, **kw: [20] if provider_id == 1 else [21],
    )
    monkeypatch.setattr(vod_importer.vod_db, "auto_merge_movie_by_tmdb", lambda movie_id: None)
    monkeypatch.setattr(vod_importer.vod_db, "auto_merge_series_by_tmdb", lambda series_id: None)

    asyncio.run(asyncio.wait_for(vod_importer.bulk_enrich_all(concurrency=8), timeout=5))

    assert call_count["movie10"] == 2, "movie phase should be retried exactly once after failing"
    assert ("movie", 10) in events
    assert ("series", 20) in events, "successful retry must unblock this provider's series phase"
    progress = vod_importer.get_enrich_progress()
    assert progress.get("providers_incomplete", []) == []


def test_provider_movie_retry_fails_again_flags_incomplete_and_skips_series(monkeypatch):
    """If the single retry also fails, Provider X's movie enrichment must be
    flagged incomplete and its series must NEVER be attempted."""
    events = []

    async def fake_enrich_movie(movie_id, *, force=False, skip_auto_merge=False):
        if movie_id == 10:
            raise RuntimeError("still broken")
        events.append(("movie", movie_id))
        return True

    async def fake_enrich_series(series_id, *, force=False, skip_auto_merge=False):
        events.append(("series", series_id))
        return {"fetched": True, "reason": None}

    monkeypatch.setattr(vod_importer, "enrich_movie", fake_enrich_movie)
    monkeypatch.setattr(vod_importer, "enrich_series", fake_enrich_series)
    monkeypatch.setattr(vod_importer.vod_db, "list_providers", lambda: [_provider(1, "ProvX"), _provider(2, "ProvY")])
    monkeypatch.setattr(
        vod_importer.vod_db, "list_all_movie_ids",
        lambda provider_id=None, **kw: [10] if provider_id == 1 else [11],
    )
    monkeypatch.setattr(
        vod_importer.vod_db, "list_all_series_ids",
        lambda provider_id=None, **kw: [20] if provider_id == 1 else [21],
    )
    monkeypatch.setattr(vod_importer.vod_db, "auto_merge_movie_by_tmdb", lambda movie_id: None)
    monkeypatch.setattr(vod_importer.vod_db, "auto_merge_series_by_tmdb", lambda series_id: None)

    asyncio.run(asyncio.wait_for(vod_importer.bulk_enrich_all(concurrency=8), timeout=5))

    assert ("series", 20) not in events, "series must never be attempted when movies never succeed"
    progress = vod_importer.get_enrich_progress()
    incomplete = progress.get("providers_incomplete", [])
    assert any(entry["provider_id"] == 1 and entry["phase"] == "movies" for entry in incomplete)


def test_provider_series_failure_does_not_rerun_movies_or_block_others(monkeypatch):
    """A series-phase failure (movies already succeeded) must not retry or
    re-run that provider's movies, and must not affect any other provider."""
    events = []

    async def fake_enrich_movie(movie_id, *, force=False, skip_auto_merge=False):
        events.append(("movie", movie_id))
        return True

    async def fake_enrich_series(series_id, *, force=False, skip_auto_merge=False):
        if series_id == 20:
            raise RuntimeError("series phase stalled")
        events.append(("series", series_id))
        return {"fetched": True, "reason": None}

    monkeypatch.setattr(vod_importer, "enrich_movie", fake_enrich_movie)
    monkeypatch.setattr(vod_importer, "enrich_series", fake_enrich_series)
    monkeypatch.setattr(vod_importer.vod_db, "list_providers", lambda: [_provider(1, "ProvX"), _provider(2, "ProvY")])
    monkeypatch.setattr(
        vod_importer.vod_db, "list_all_movie_ids",
        lambda provider_id=None, **kw: [10] if provider_id == 1 else [11],
    )
    monkeypatch.setattr(
        vod_importer.vod_db, "list_all_series_ids",
        lambda provider_id=None, **kw: [20] if provider_id == 1 else [21],
    )
    monkeypatch.setattr(vod_importer.vod_db, "auto_merge_movie_by_tmdb", lambda movie_id: None)
    monkeypatch.setattr(vod_importer.vod_db, "auto_merge_series_by_tmdb", lambda series_id: None)

    asyncio.run(asyncio.wait_for(vod_importer.bulk_enrich_all(concurrency=8), timeout=5))

    # Movies for provider X only ran once -- no re-run triggered by the
    # series failure.
    assert events.count(("movie", 10)) == 1
    assert ("series", 21) in events, "provider Y unaffected by provider X's series failure"
    progress = vod_importer.get_enrich_progress()
    incomplete = progress.get("providers_incomplete", [])
    assert any(entry["provider_id"] == 1 and entry["phase"] == "series" for entry in incomplete)


def test_movie_auto_merge_runs_once_after_all_providers_resolve_movies(monkeypatch):
    """Auto-merge for movies must fire exactly once, after every provider's
    movie phase has resolved (not per-item, not per-provider)."""
    merge_calls = []

    async def fake_enrich_movie(movie_id, *, force=False, skip_auto_merge=False):
        assert skip_auto_merge is True, "bulk enrich must suppress the inline per-item merge"
        return True

    async def fake_enrich_series(series_id, *, force=False, skip_auto_merge=False):
        return {"fetched": True, "reason": None}

    def fake_auto_merge_movie(movie_id):
        merge_calls.append(movie_id)

    monkeypatch.setattr(vod_importer, "enrich_movie", fake_enrich_movie)
    monkeypatch.setattr(vod_importer, "enrich_series", fake_enrich_series)
    monkeypatch.setattr(vod_importer.vod_db, "list_providers", lambda: [_provider(1, "ProvA"), _provider(2, "ProvB")])
    monkeypatch.setattr(
        vod_importer.vod_db, "list_all_movie_ids",
        lambda provider_id=None, **kw: [10] if provider_id == 1 else [11],
    )
    monkeypatch.setattr(
        vod_importer.vod_db, "list_all_series_ids",
        lambda provider_id=None, **kw: [20] if provider_id == 1 else [21],
    )
    monkeypatch.setattr(vod_importer.vod_db, "auto_merge_movie_by_tmdb", fake_auto_merge_movie)
    monkeypatch.setattr(vod_importer.vod_db, "auto_merge_series_by_tmdb", lambda series_id: None)

    asyncio.run(vod_importer.bulk_enrich_all(concurrency=8))

    assert sorted(merge_calls) == [10, 11]
