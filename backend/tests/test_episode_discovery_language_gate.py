"""Episode lists are only fetched for sources in an enabled playback language.

A series kept visible by an English source used to queue its other-language
sources (e.g. a provider's Spanish copy) for an episode-list fetch, even
though playback filters those sources out. Each one cost a provider call.
"""

import config


def _seed(db):
    provider_id = db.upsert_provider("XC-Test", "http://xc.example.com", "user", "pass", provider_type="xc")
    db.bulk_import_series(provider_id, [{
        "name": f"Show {i}", "year": 2020, "provider_series_id": str(i),
        "provider_category_name": None, "raw_name": f"Show {i}", "_has_detail": True,
        "genre": None, "description": None, "cast_list": None, "director": None,
        "poster_url": None, "rating": None, "release_date": None, "tmdb_id": None,
        "provider_last_modified": None,
    } for i in range(3)])
    rows = db.list_pending_series_sources(provider_id)
    conn = db._connect()
    conn.execute("UPDATE series_sources SET language='ES' WHERE id=?", (rows[0]["id"],))
    conn.execute("UPDATE series_sources SET language=NULL WHERE id=?", (rows[1]["id"],))  # untagged = EN
    conn.commit()
    conn.close()
    return provider_id, rows


def test_disabled_language_sources_are_not_queued(db):
    config.save_enabled_languages(["EN"])
    provider_id, rows = _seed(db)
    ids = [r["id"] for r in db.list_pending_series_sources(provider_id)]
    assert ids == [rows[1]["id"], rows[2]["id"]]


def test_enabling_the_language_queues_them_again(db):
    config.save_enabled_languages(["EN", "ES"])
    provider_id, _ = _seed(db)
    assert len(db.list_pending_series_sources(provider_id)) == 3


def test_provider_with_only_disabled_language_sources_has_no_pending_work(db):
    config.save_enabled_languages(["EN"])
    provider_id, _ = _seed(db)
    conn = db._connect()
    conn.execute("UPDATE series_sources SET language='ES' WHERE provider_id=?", (provider_id,))
    conn.commit()
    conn.close()
    assert db.has_pending_series_source_enrichment(provider_id) is False
