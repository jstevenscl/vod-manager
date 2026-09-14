"""Plan-doc follow-up (2026-09-14): "make a provider lane fetch only that
provider's series source". _run_provider_series_phase selects series_ids
scoped to ONE provider (via series_sources, fixed same day -- see
test_series_source_enrichment_gate.py's
test_provider_series_selection_includes_series_with_no_episodes_yet), but
used to hand each id to enrich_series(series_id), which loops EVERY source
attached to that series_id regardless of which provider's lane is running.
A series carried by both Provider A and Provider B would have Provider A's
bulk lane also fetch Provider B's get_series_info -- weakening the intended
per-provider request/concurrency isolation and risking duplicate work before
the source's own freshness stamp is written.

Fix: enrich_series_source_only(series_id, provider_id, ...) fetches only the
one series_sources row matching provider_id, never touching any other
provider's source for the same series. enrich_series(series_id) is left
unchanged as the on-demand/UI-facing all-sources wrapper. The bulk
coordinator (_run_provider_series_phase, via _enrich_one) now calls the new
provider-scoped helper instead of enrich_series."""

import asyncio

import pytest

import vod_importer


def _import_series_with_two_sources(db, name="Shared Show", year=2020):
    """Two providers importing catalog entries for the identically-named/
    -year series get matched to the SAME series_id, each with its own
    series_sources row -- this is the real multi-provider-failover shape
    (series_sources, 2026-09-09) that makes cross-provider leakage possible."""
    provider_a = db.upsert_provider("Provider A", "http://a.example.com", "user", "pass", provider_type="xc")
    provider_b = db.upsert_provider("Provider B", "http://b.example.com", "user", "pass", provider_type="xc")

    def _item(pid_str):
        return {
            "name": name, "year": year, "provider_series_id": pid_str,
            "provider_category_name": None, "raw_name": name, "_has_detail": True,
            "genre": None, "description": None, "cast_list": None, "director": None,
            "poster_url": None, "rating": None, "release_date": None, "tmdb_id": None,
            "provider_last_modified": None,
        }

    db.bulk_import_series(provider_a, [_item("a-1")])
    db.bulk_import_series(provider_b, [_item("b-1")])

    series_id = db.list_series(limit=10)[0]["id"]
    sources = db.list_series_sources(series_id)
    assert len(sources) == 2, "both providers must attach to the same series_id"
    return provider_a, provider_b, series_id


def test_enrich_series_source_only_never_fetches_a_different_providers_source(db, monkeypatch):
    provider_a, provider_b, series_id = _import_series_with_two_sources(db)

    fetched_provider_ids = []

    class FakeClient:
        def __init__(self, provider):
            fetched_provider_ids.append(provider["id"])

        async def get_series_info(self, provider_series_id):
            return {"info": {}, "episodes": {}}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(vod_importer, "XCProviderClient", FakeClient)

    result = asyncio.run(
        vod_importer.enrich_series_source_only(series_id, provider_a, force=True, skip_auto_merge=True)
    )

    assert result["fetched"] is True
    assert fetched_provider_ids == [provider_a], (
        "a provider-scoped series enrichment call must only ever construct a "
        "client for (and fetch from) the ONE provider it was scoped to -- "
        f"got client construction(s) for provider id(s) {fetched_provider_ids}, "
        f"expected only [{provider_a}]"
    )


def test_bulk_series_phase_only_touches_its_own_provider_source(db, monkeypatch):
    """End-to-end through the real bulk coordinator (_run_provider_series_phase
    / _enrich_one), not just the new helper in isolation -- proves the wiring,
    not just the function."""
    provider_a, provider_b, series_id = _import_series_with_two_sources(db)

    fetched_provider_ids = []

    class FakeClient:
        def __init__(self, provider):
            fetched_provider_ids.append(provider["id"])

        async def get_series_info(self, provider_series_id):
            return {"info": {}, "episodes": {}}

    monkeypatch.setattr(vod_importer, "XCProviderClient", FakeClient)

    sem = asyncio.Semaphore(4)
    ok, series_ids = asyncio.run(
        vod_importer._run_provider_series_phase({"id": provider_a, "name": "Provider A", "provider_type": "xc"}, sem, force=True)
    )

    assert ok is True
    assert series_ids == [series_id]
    assert fetched_provider_ids == [provider_a], (
        "Provider A's bulk series phase must not fetch Provider B's source "
        f"for a series shared between them -- got {fetched_provider_ids}"
    )
