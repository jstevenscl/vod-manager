"""Known TMDB identities are resolved without provider detail requests."""

import asyncio

import tmdb_sync
import vod_importer


def _movie(stream_id: str, tmdb_id: str | None) -> dict:
    return {
        "name": f"Movie {stream_id}", "year": 2024,
        "provider_stream_id": stream_id, "container_extension": "mp4",
        "tmdb_id": tmdb_id,
    }


def test_pending_movie_selectors_split_tmdb_from_provider_fallback(db):
    provider_id = db.upsert_provider("Example Provider", "http://example.invalid", "user", "pass")
    db.bulk_import_movies(provider_id, [_movie("known", "123"), _movie("unknown", None)])

    assert len(db.list_movie_ids_pending_tmdb_enrichment()) == 1
    assert len(db.list_movie_ids_pending_provider_enrichment(provider_id)) == 1


def test_tmdb_job_writes_known_identity_without_provider_client(db, monkeypatch):
    provider_id = db.upsert_provider("Example Provider", "http://example.invalid", "user", "pass")
    db.bulk_import_movies(provider_id, [_movie("known", "123")])
    movie_id = db.list_movie_ids_pending_tmdb_enrichment()[0]

    async def fake_tmdb(tmdb_id):
        assert tmdb_id == "123"
        return {
            "name": "Resolved Movie", "genre": "Drama", "description": "summary",
            "cast_list": "Actor", "director": "Director", "country": "US",
            "poster_url": "https://image.example/poster.jpg", "duration_secs": 6000,
            "rating": 7.0, "release_date": "2024-01-01", "content_rating": "PG",
        }

    monkeypatch.setattr(tmdb_sync, "get_movie_full_details", fake_tmdb)
    monkeypatch.setattr(vod_importer.vod_db, "get_active_rules_for_field", lambda *_: [])
    asyncio.run(vod_importer.bulk_enrich_tmdb_movies(concurrency=1))

    movie = db.get_movie(movie_id)
    assert movie["genre"] == "Drama"
    assert movie["last_enriched_at"] is not None
    assert db.list_movie_ids_pending_tmdb_enrichment() == []

