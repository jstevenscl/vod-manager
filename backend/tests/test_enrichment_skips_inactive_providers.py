"""vod_manager-7rc: enrichment must not call a deactivated provider's API."""

import asyncio

import pytest

import vod_importer


def _provider_called(*args, **kwargs):
    raise AssertionError("an inactive provider's API must not be touched")


def test_movie_enrichment_skips_a_movie_only_carried_by_inactive_providers(db, monkeypatch):
    provider = db.upsert_provider("off", "http://example.com", "u", "p")
    db.bulk_import_movies(provider, [{"name": "Heat", "year": 1995, "provider_stream_id": "m1",
                                      "container_extension": "mkv", "raw_name": "Heat", "_has_detail": True}])
    movie_id = db.get_movie_by_name_year("Heat", 1995)["id"]
    db.set_provider_active(provider, False)
    monkeypatch.setattr(vod_importer.vod_db, "get_provider", _provider_called)

    assert asyncio.run(vod_importer.enrich_movie(movie_id, force=True)) is False


def test_movie_enrichment_prefers_the_active_provider(db):
    off = db.upsert_provider("aaa-off", "http://example.com/a", "u", "p")
    on = db.upsert_provider("zzz-on", "http://example.com/z", "u", "p")
    for provider in (off, on):
        db.bulk_import_movies(provider, [{"name": "Heat", "year": 1995, "provider_stream_id": f"m{provider}",
                                          "container_extension": "mkv", "raw_name": "Heat", "_has_detail": True}])
    movie_id = db.get_movie_by_name_year("Heat", 1995)["id"]
    db.set_provider_active(off, False)

    assert db.list_movie_sources(movie_id)[0]["provider_id"] == on


def test_series_enrichment_skips_a_series_only_carried_by_inactive_providers(db, monkeypatch):
    provider = db.upsert_provider("off", "http://example.com", "u", "p")
    series_id = db.upsert_series("Dark", 2017)
    conn = db._connect()
    try:
        conn.execute("INSERT INTO series_sources (series_id, provider_id, provider_series_id, raw_name, added_at, last_seen_at) "
                     "VALUES (?,?,?,?,?,?)", (series_id, provider, "s1", "Dark", db._now(), db._now()))
        conn.commit()
    finally:
        conn.close()
    db.set_provider_active(provider, False)
    monkeypatch.setattr(vod_importer.vod_db, "get_provider", _provider_called)

    result = asyncio.run(vod_importer.enrich_series(series_id, force=True))

    assert result["fetched"] is False and "inactive" in result["reason"]
