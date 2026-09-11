"""bulk_import_plex_series wrote series/episodes/episode_sources but never a
series_sources row for the series itself -- unlike its sibling
bulk_import_plex_movies, which does upsert movie_sources. Found live
2026-09-11: Plex-109 (an active, refreshed provider) had 553 movie_sources
rows but ZERO series_sources rows, so it silently never appeared as a
provider anywhere find_duplicate_groups/enrich_series read series_sources
(duplicate finder provider badges, source counts, multi-provider episode
failover) -- and any series carried only by Plex showed a misleading "0
sources" in the Duplicate Finder even though it was actively carried.
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
