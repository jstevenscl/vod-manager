"""vod_manager-d5t: movie re-enrichment backs off while it keeps finding nothing
new. A blind every-TTL refetch re-requested ~every movie from the provider/TMDB
although ~91% of re-fetches (measured on a real catalog) returned identical data."""

import time

import vod_db

DAY = 86400
FIELDS = {"genre": "Drama", "description": "A plot.", "director": "Someone", "rating": "7.1"}


def _movie(db, name="Backoff Movie"):
    provider_id = db.upsert_provider("prov1", "http://example.com", "user", "pass")
    db.bulk_import_movies(provider_id, [{
        "name": name, "year": 2001, "provider_stream_id": "s1", "container_extension": "mp4",
        "raw_name": name, "_has_detail": True,
    }])
    return db.get_movie_by_name_year(name, 2001)["id"]


def _age(db, movie_id, days):
    """Pretend the last enrichment happened `days` ago."""
    conn = vod_db._connect()
    conn.execute("UPDATE movies SET last_enriched_at=? WHERE id=?", (str(time.time() - days * DAY - 60), movie_id))
    conn.commit()
    conn.close()


def _count(db, movie_id):
    return db.get_movie(movie_id)["enrich_stable_count"]


def test_never_enriched_movie_is_stale(db):
    movie_id = _movie(db)
    assert db.movie_needs_enrichment(movie_id)


def test_unchanged_reenrichment_backs_off_and_change_resets(db):
    movie_id = _movie(db)
    db.set_movie_enrichment(movie_id, **FIELDS)
    assert _count(db, movie_id) == 0  # first fill: the fields were new, so no backoff yet
    db.set_movie_enrichment(movie_id, **FIELDS)
    assert _count(db, movie_id) == 1  # identical re-fetch -> +1
    db.set_movie_enrichment(movie_id, **FIELDS)
    assert _count(db, movie_id) == 2
    db.set_movie_enrichment(movie_id, **{**FIELDS, "rating": "7.4"})
    assert _count(db, movie_id) == 0  # a real change resets the backoff


def test_values_the_provider_did_not_send_are_not_a_change(db):
    movie_id = _movie(db)
    db.set_movie_enrichment(movie_id, **FIELDS)
    db.set_movie_enrichment(movie_id, **FIELDS)
    before = _count(db, movie_id)
    db.set_movie_enrichment(movie_id, genre=None, description="")  # provider sent nothing
    assert _count(db, movie_id) == before + 1


def test_effective_ttl_doubles_per_unchanged_check_up_to_the_cap(db):
    movie_id = _movie(db)
    db.set_movie_enrichment(movie_id, **FIELDS)
    for stable, expected_days in [(0, 1), (1, 2), (2, 4), (3, 8), (4, 16), (9, 16)]:
        conn = vod_db._connect()
        conn.execute("UPDATE movies SET enrich_stable_count=? WHERE id=?", (stable, movie_id))
        conn.commit()
        conn.close()
        _age(db, movie_id, expected_days * 0.9)
        assert not db.movie_needs_enrichment(movie_id), (stable, "should still be fresh")
        _age(db, movie_id, expected_days * 1.1)
        assert db.movie_needs_enrichment(movie_id), (stable, "should be due")


def test_batch_writer_applies_the_same_backoff(db):
    movie_id = _movie(db)
    db.apply_movie_enrichment_batch([{"movie_id": movie_id, "fields": dict(FIELDS)}])
    db.apply_movie_enrichment_batch([{"movie_id": movie_id, "fields": dict(FIELDS)}])
    db.apply_movie_enrichment_batch([{"movie_id": movie_id, "fields": dict(FIELDS)}])
    assert _count(db, movie_id) == 2
    db.apply_movie_enrichment_batch([{"movie_id": movie_id, "fields": {**FIELDS, "description": "Changed plot."}}])
    assert _count(db, movie_id) == 0


def test_series_staleness_is_unchanged(db):
    assert vod_db._is_stale(None) is True
    assert vod_db._is_stale(str(time.time() - 2 * DAY)) is True       # default TTL 24h, no backoff
    assert vod_db._is_stale(str(time.time() - 3600)) is False


def test_enrich_movie_skips_the_provider_call_while_backed_off(db, monkeypatch):
    """End to end through vod_importer.enrich_movie with a fake XC provider."""
    import asyncio
    import vod_importer

    movie_id = _movie(db)
    calls = []

    class FakeClient:
        def __init__(self, _provider):
            pass

        async def get_vod_info(self, stream_id):
            calls.append(stream_id)
            return {"info": {"genre": "Drama", "plot": "A plot.", "director": "Someone", "rating": "7.1"}}

    monkeypatch.setattr(vod_importer, "XCProviderClient", FakeClient)
    monkeypatch.setattr(vod_importer.vod_db, "get_active_rules_for_field", lambda *_: [])

    assert asyncio.run(vod_importer.enrich_movie(movie_id)) is True           # first fill
    assert len(calls) == 1
    assert asyncio.run(vod_importer.enrich_movie(movie_id)) is False          # fresh: skipped
    _age(db, movie_id, 1.5)
    assert asyncio.run(vod_importer.enrich_movie(movie_id)) is True           # >24h, count 0 -> due
    assert len(calls) == 2 and _count(db, movie_id) == 1                      # identical -> backoff 1
    _age(db, movie_id, 1.5)
    assert asyncio.run(vod_importer.enrich_movie(movie_id)) is False          # 1.5d < 2d: backed off
    assert len(calls) == 2
    _age(db, movie_id, 2.5)
    assert asyncio.run(vod_importer.enrich_movie(movie_id)) is True           # >2d -> due again
    assert len(calls) == 3
    assert asyncio.run(vod_importer.enrich_movie(movie_id, force=True)) is True  # force bypasses
    assert len(calls) == 4
