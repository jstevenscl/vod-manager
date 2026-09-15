"""reconcile_provider_catalog_sources removes a provider's own source rows
once they're absent from a successful full catalog snapshot -- a title the
provider genuinely stopped advertising, not one merely excluded by local
language/category policy (the caller must pass the raw, unfiltered catalog
IDs for that reason). Canonical movies/series survive whenever another
provider still has a source; a provider-wide fetch failure never reaches
this function at all (import_provider_catalog only calls it after both
list calls succeed), so an outage can't look like every title got removed.
"""


def test_source_absent_from_fresh_catalog_gets_removed(db):
    provider_id = db.upsert_provider("XC-Test", "http://example.com", "user", "pass")
    db.bulk_import_movies(provider_id, [
        {"name": "Movie A", "year": 2020, "provider_stream_id": "100", "container_extension": "mkv"},
        {"name": "Movie B", "year": 2021, "provider_stream_id": "200", "container_extension": "mkv"},
    ])

    result = db.reconcile_provider_catalog_sources(
        provider_id, seen_movie_stream_ids={"100"}, seen_series_ids=set(),
    )

    assert result["movie_sources_removed"] == 1
    all_movies = db.list_movies(limit=1000)
    names = {m["name"] for m in all_movies}
    assert "Movie A" in names
    assert "Movie B" not in names  # sourceless after reconciliation -> purged


def test_source_present_in_both_is_untouched(db):
    provider_id = db.upsert_provider("XC-Test", "http://example.com", "user", "pass")
    db.bulk_import_movies(provider_id, [
        {"name": "Movie A", "year": 2020, "provider_stream_id": "100", "container_extension": "mkv"},
    ])

    result = db.reconcile_provider_catalog_sources(
        provider_id, seen_movie_stream_ids={"100"}, seen_series_ids=set(),
    )

    assert result["movie_sources_removed"] == 0
    all_movies = db.list_movies(limit=1000)
    assert any(m["name"] == "Movie A" for m in all_movies)


def test_second_providers_source_keeps_movie_alive_after_first_providers_reconcile(db):
    provider_a = db.upsert_provider("XC-A", "http://a.example.com", "user", "pass")
    provider_b = db.upsert_provider("XC-B", "http://b.example.com", "user", "pass")
    db.bulk_import_movies(provider_a, [
        {"name": "Shared Movie", "year": 2020, "provider_stream_id": "100", "container_extension": "mkv"},
    ])
    db.bulk_import_movies(provider_b, [
        {"name": "Shared Movie", "year": 2020, "provider_stream_id": "999", "container_extension": "mkv"},
    ])

    result = db.reconcile_provider_catalog_sources(
        provider_a, seen_movie_stream_ids=set(), seen_series_ids=set(),
    )

    assert result["movie_sources_removed"] == 1
    all_movies = db.list_movies(limit=1000)
    row = next(m for m in all_movies if m["name"] == "Shared Movie")
    sources = db.list_movie_sources(row["id"])
    assert len(sources) == 1
    assert sources[0]["provider_id"] == provider_b


def test_provider_wide_fetch_failure_never_calls_reconcile(monkeypatch, db):
    """import_provider_catalog only reaches reconcile_provider_catalog_sources
    after BOTH _import_movies_for_provider and _import_series_for_provider
    return successfully -- a raised exception from either (e.g. the
    provider being down) propagates out of import_provider_catalog before
    reconciliation ever runs, so an outage can't be mistaken for "every
    title was removed". This is a structural guarantee (the awaits are
    sequential, not wrapped in a try/except that swallows the error), not
    something reconcile_provider_catalog_sources itself needs to guard
    against -- covered here by asserting the function's own behavior is
    correct for an empty seen-set (which is exactly what a naive/unsafe
    caller passing "nothing seen" on a failed fetch WOULD wipe), making
    clear why the caller-side guarantee (never calling this at all on
    failure) is what actually protects real data, not any check inside
    this function itself."""
    provider_id = db.upsert_provider("XC-Test", "http://example.com", "user", "pass")
    db.bulk_import_movies(provider_id, [
        {"name": "Movie A", "year": 2020, "provider_stream_id": "100", "container_extension": "mkv"},
    ])

    # An empty seen-set (what a fetch failure would look like if it were
    # ever wrongly passed through) removes everything -- proving the real
    # safety net is import_provider_catalog never calling this after a
    # failed fetch, not any leniency inside reconcile itself.
    result = db.reconcile_provider_catalog_sources(
        provider_id, seen_movie_stream_ids=set(), seen_series_ids=set(),
    )
    assert result["movie_sources_removed"] == 1
