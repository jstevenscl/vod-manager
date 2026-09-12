"""User report (2026-09-12): non-Plex series enrichment silently skipped
episode fetching for essentially every series across every XC provider (WOBO,
WarpTV, AMBER BABY, AMBER BABY 2) despite bulk enrich completing "0 errors" --
episodes stayed at 0. Root cause: series_sources.last_seen_at was overloaded
with two unrelated meanings. bulk_import_series's cheap catalog-list refresh
(list_series/get_series_categories only, no episode data) upserts last_seen_at
on every provider refresh cycle just to confirm the source still exists in the
catalog. series_source_needs_enrichment (the gate enrich_series uses to decide
whether a real get_series_info/episode fetch is even worth attempting) reads
that SAME column, assuming it only ever means "episodes were actually fetched
this recently". Since catalog refreshes run far more often than a full bulk
enrich, last_seen_at kept looking "fresh" well before episodes were ever
fetched for that source -- so the per-source TTL check falsely reported "not
stale, skip" and get_series_info was never called at all for these sources.
Confirmed live: force-running enrich_series on an affected series immediately
fetched and wrote its 60 real episodes with no code changes -- the fetch path
itself was never broken, only the gate deciding whether to attempt it.

Fix: give the enrichment gate its own column, episodes_last_enriched_at,
written ONLY by set_series_source_enrichment (a real successful episode
fetch), never touched by the catalog-list import's last_seen_at upsert.
last_seen_at keeps its original meaning (source-quality/freshness ordering,
see vod_db.py's _best_source_cte usage) unchanged."""


def test_series_source_needs_enrichment_true_for_never_enriched_source(db):
    provider_id = db.upsert_provider("XC-Test", "http://xc.example.com", "user", "pass", provider_type="xc")
    db.bulk_import_series(provider_id, [{
        "name": "Some Show", "year": 2020, "provider_series_id": "1",
        "provider_category_name": None, "raw_name": "Some Show", "_has_detail": True,
        "genre": None, "description": None, "cast_list": None, "director": None,
        "poster_url": None, "rating": None, "release_date": None, "tmdb_id": None,
        "provider_last_modified": None,
    }])
    sources = db.list_series_sources(db.list_series(limit=10)[0]["id"])
    assert db.series_source_needs_enrichment(sources[0]) is True


def test_catalog_reimport_does_not_suppress_enrichment_need(db):
    """The actual bug: a plain re-import of the catalog list (no episodes
    fetched) must NOT make series_source_needs_enrichment start returning
    False -- only a real set_series_source_enrichment call should do that."""
    provider_id = db.upsert_provider("XC-Test", "http://xc.example.com", "user", "pass", provider_type="xc")
    item = {
        "name": "Some Show", "year": 2020, "provider_series_id": "1",
        "provider_category_name": None, "raw_name": "Some Show", "_has_detail": True,
        "genre": None, "description": None, "cast_list": None, "director": None,
        "poster_url": None, "rating": None, "release_date": None, "tmdb_id": None,
        "provider_last_modified": None,
    }
    db.bulk_import_series(provider_id, [item])
    series_id = db.list_series(limit=10)[0]["id"]
    sources = db.list_series_sources(series_id)
    assert db.series_source_needs_enrichment(sources[0]) is True

    # Simulate a later, unrelated catalog refresh re-importing the same
    # series (this is what a periodic XC refresh does constantly -- no
    # episodes involved at all).
    db.bulk_import_series(provider_id, [item])
    sources = db.list_series_sources(series_id)
    assert db.series_source_needs_enrichment(sources[0]) is True, (
        "a catalog-list-only re-import must not make the episode-enrichment "
        "gate think episodes were already fetched"
    )


def test_set_series_source_enrichment_clears_the_need(db):
    provider_id = db.upsert_provider("XC-Test", "http://xc.example.com", "user", "pass", provider_type="xc")
    db.bulk_import_series(provider_id, [{
        "name": "Some Show", "year": 2020, "provider_series_id": "1",
        "provider_category_name": None, "raw_name": "Some Show", "_has_detail": True,
        "genre": None, "description": None, "cast_list": None, "director": None,
        "poster_url": None, "rating": None, "release_date": None, "tmdb_id": None,
        "provider_last_modified": None,
    }])
    series_id = db.list_series(limit=10)[0]["id"]
    sources = db.list_series_sources(series_id)

    db.set_series_source_enrichment(series_id, provider_id, "1")

    sources = db.list_series_sources(series_id)
    assert db.series_source_needs_enrichment(sources[0]) is False
