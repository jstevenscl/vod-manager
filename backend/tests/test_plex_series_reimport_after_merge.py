"""A Plex/Emby/library series merged into another card must be recognised on
the next import through its series_sources link -- otherwise the importer
creates a fresh duplicate card every run and the merge undoes it again."""


def _plex_card_merged_into_other(db):
    xc = db.upsert_provider("Provider One", "http://one.invalid", "u", "p")
    plex = db.upsert_provider("Library", "http://plex.invalid", "u", "p")
    db.bulk_import_series(xc, [{
        "name": "Example Show", "year": 2025, "provider_series_id": "xc-1",
        "tmdb_id": None, "provider_category_name": None, "raw_name": "Example Show",
        "_has_detail": True,
    }])
    plex_item = {
        "name": "Example Show (2025)", "year": 2025, "provider_series_id": "plex-1",
        "episodes": [{"season_number": 1, "episode_number": 1, "name": "Pilot",
                      "provider_stream_id": "plex-ep-1", "container_extension": "mkv"}],
    }
    db.bulk_import_plex_series(plex, [plex_item])
    by_name = {r["name"]: r["id"] for r in db.list_series(limit=10)}
    keep_id, drop_id = by_name["Example Show"], by_name["Example Show (2025)"]
    db.merge_series(drop_id, keep_id)
    return plex, plex_item, keep_id


def test_plex_reimport_after_merge_creates_nothing_new(db):
    plex, plex_item, keep_id = _plex_card_merged_into_other(db)

    result = db.bulk_import_plex_series(plex, [plex_item])

    assert [r["id"] for r in db.list_series(limit=10)] == [keep_id]
    assert result["series_created"] == 0


def test_plex_reimport_after_merge_keeps_source_on_survivor(db):
    plex, plex_item, keep_id = _plex_card_merged_into_other(db)

    db.bulk_import_plex_series(plex, [plex_item])

    sources = db.list_series_sources(keep_id)
    assert sorted(s["provider_series_id"] for s in sources) == ["plex-1", "xc-1"]
