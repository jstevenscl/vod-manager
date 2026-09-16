"""Provider-free trailer enrichment regression coverage."""

import asyncio

import tmdb_sync
import vod_importer


def test_tmdb_prefers_official_youtube_trailer():
    picked = tmdb_sync._select_youtube_trailer({"videos": {"results": [
        {"site": "Vimeo", "key": "v", "type": "Trailer", "official": True},
        {"site": "YouTube", "key": "teaser", "type": "Teaser", "official": False},
        {"site": "YouTube", "key": "official", "type": "Trailer", "official": True},
    ]}})
    assert picked == {"key": "official", "site": "YouTube"}


def test_trailer_status_retries_once_then_confirms_absence(db):
    provider = db.upsert_provider("Synthetic Provider", "http://example.invalid", "u", "p")
    db.bulk_import_movies(provider, [{"name": "Fixture Movie", "year": 2024,
        "provider_stream_id": "m1", "container_extension": "mp4", "tmdb_id": "101"}])
    movie_id = db.list_all_movie_ids()[0]
    db.record_trailer_result("movie", movie_id)
    assert db.get_movie(movie_id)["trailer_status"] == "not_found"
    db.record_trailer_result("movie", movie_id)
    row = db.get_movie(movie_id)
    assert row["trailer_status"] == "confirmed_none"
    assert "no YouTube trailer" in row["trailer_last_error"]


def test_backfill_records_movie_success_and_series_failure(db, monkeypatch):
    provider = db.upsert_provider("Synthetic Provider", "http://example.invalid", "u", "p")
    db.bulk_import_movies(provider, [{"name": "Fixture Movie", "year": 2024,
        "provider_stream_id": "m1", "container_extension": "mp4", "tmdb_id": "101"}])
    db.bulk_import_series(provider, [{"name": "Fixture Series", "year": 2024,
        "provider_series_id": "s1", "tmdb_id": "202", "provider_category_name": None,
        "raw_name": "Fixture Series", "_has_detail": True}])
    async def movie(_): return {"ok": True, "trailer": {"key": "movie-key", "site": "YouTube"}}
    async def series(_): return {"ok": False, "error": "synthetic timeout"}
    monkeypatch.setattr(tmdb_sync, "get_movie_trailer", movie)
    monkeypatch.setattr(tmdb_sync, "get_tv_trailer", series)
    asyncio.run(vod_importer.bulk_enrich_missing_trailers(concurrency=2))
    m = db.get_movie(db.list_all_movie_ids()[0])
    s = db.list_series(limit=10)[0]
    assert (m["trailer_status"], m["trailer_key"]) == ("found", "movie-key")
    assert s["trailer_status"] == "error"
    assert "synthetic timeout" in s["trailer_last_error"]
