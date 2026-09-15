"""bulk_import_plex_series wrote series/episodes/episode_sources but never a
series_sources row for the series itself -- unlike its sibling
bulk_import_plex_movies, which does upsert movie_sources. Any series carried
only by Plex would show a misleading "0 sources" in the Duplicate Finder
even though it was actively carried, and enrich_series (which now loops
over series_sources rows, not just series.import_provider_id) would never
see a Plex-only series as having a source at all.
"""


def test_bulk_import_plex_series_creates_series_sources_row(db):
    provider_id = db.upsert_provider("Plex-Test", "http://plex.example.com", "", "token", provider_type="plex")

    result = db.bulk_import_plex_series(provider_id, [
        {
            "name": "House of the Dragon",
            "year": 2022,
            "provider_series_id": "48740",
            "genre": None,
            "description": None,
            "director": None,
            "cast_list": None,
            "poster_url": None,
            "tmdb_id": "94997",
            "rating": None,
            "release_date": None,
            "last_enriched_at": "0",
            "provider_category_name": "TV Shows",
            "episodes": [],
        },
    ])

    assert result["series_created"] == 1
    all_series = db.list_series(limit=1000)
    row = next(s for s in all_series if s["name"] == "House of the Dragon" and s["year"] == 2022)

    sources = db.list_series_sources(row["id"])
    assert len(sources) == 1
    assert sources[0]["provider_id"] == provider_id
    assert sources[0]["provider_series_id"] == "48740"
    assert sources[0]["provider_category_name"] == "TV Shows"


def test_bulk_import_plex_series_re_import_upserts_not_duplicates(db):
    provider_id = db.upsert_provider("Plex-Test", "http://plex.example.com", "", "token", provider_type="plex")

    item = {
        "name": "House of the Dragon",
        "year": 2022,
        "provider_series_id": "48740",
        "genre": None,
        "description": None,
        "director": None,
        "cast_list": None,
        "poster_url": None,
        "tmdb_id": "94997",
        "rating": None,
        "release_date": None,
        "last_enriched_at": "0",
        "provider_category_name": "TV Shows",
        "episodes": [],
    }
    db.bulk_import_plex_series(provider_id, [item])
    result2 = db.bulk_import_plex_series(provider_id, [item])

    assert result2["series_matched"] == 1
    all_series = db.list_series(limit=1000)
    matching = [s for s in all_series if s["name"] == "House of the Dragon" and s["year"] == 2022]
    assert len(matching) == 1

    sources = db.list_series_sources(matching[0]["id"])
    assert len(sources) == 1


def test_bulk_import_series_xc_creates_series_sources_row(db):
    """XC counterpart -- bulk_import_series (not the Plex-specific
    variant) also stamps a series_sources row per matching provider now,
    mirroring movie_sources."""
    provider_id = db.upsert_provider("XC-Test", "http://xc.example.com", "user", "pass", provider_type="xc")

    result = db.bulk_import_series(provider_id, [
        {
            "name": "Severance",
            "year": 2022,
            "provider_series_id": "9001",
            "provider_category_name": "TV Shows",
            "raw_name": "Severance",
        },
    ])

    assert result["series_created"] == 1
    all_series = db.list_series(limit=1000)
    row = next(s for s in all_series if s["name"] == "Severance" and s["year"] == 2022)

    sources = db.list_series_sources(row["id"])
    assert len(sources) == 1
    assert sources[0]["provider_id"] == provider_id
    assert sources[0]["provider_series_id"] == "9001"


def test_second_provider_adds_second_series_source_not_duplicate_series(db):
    """Two providers carrying the same series (matched by name+year) should
    produce ONE series row with TWO series_sources rows -- the actual
    multi-provider failover this table exists for."""
    provider_a = db.upsert_provider("XC-A", "http://a.example.com", "user", "pass", provider_type="xc")
    provider_b = db.upsert_provider("XC-B", "http://b.example.com", "user", "pass", provider_type="xc")

    db.bulk_import_series(provider_a, [
        {"name": "Severance", "year": 2022, "provider_series_id": "111", "provider_category_name": "TV", "raw_name": "Severance"},
    ])
    db.bulk_import_series(provider_b, [
        {"name": "Severance", "year": 2022, "provider_series_id": "222", "provider_category_name": "TV", "raw_name": "Severance"},
    ])

    all_series = db.list_series(limit=1000)
    matching = [s for s in all_series if s["name"] == "Severance" and s["year"] == 2022]
    assert len(matching) == 1

    sources = db.list_series_sources(matching[0]["id"])
    assert len(sources) == 2
    assert {s["provider_id"] for s in sources} == {provider_a, provider_b}
