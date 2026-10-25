"""bulk_enrich_all must not ask "is this title still fresh?" through the shared thread pool once per title: on a
catalog of hundreds of thousands that starved the API (vod_manager-051). Fresh titles are dropped in one pass."""

import asyncio
import time

import vod_db
import vod_importer


def _stamp(db, movie_id, when):
    conn = db._connect()
    conn.execute("UPDATE movies SET last_enriched_at=? WHERE id=?", (when, movie_id))
    db._commit_with_retry(conn)
    conn.close()


def test_filter_keeps_only_titles_that_are_due(db):
    fresh = db.upsert_movie("Fresh", 2020)
    stale = db.upsert_movie("Stale", 2021)
    never = db.upsert_movie("Never", 2022)
    _stamp(db, fresh, time.time())
    _stamp(db, stale, 1.0)
    assert sorted(db.filter_ids_needing_enrichment("movie", [fresh, stale, never])) == sorted([stale, never])


def test_filter_keeps_an_id_whose_check_fails(db, monkeypatch):
    def boom(_id):
        raise RuntimeError("db hiccup")
    monkeypatch.setattr(vod_db, "movie_needs_enrichment", boom)
    assert vod_db.filter_ids_needing_enrichment("movie", [1, 2]) == [1, 2]


def test_bulk_enrich_only_visits_due_titles_and_still_reports_full_totals(db, monkeypatch):
    fresh = db.upsert_movie("Fresh", 2020)
    stale = db.upsert_movie("Stale", 2021)
    _stamp(db, fresh, time.time())
    _stamp(db, stale, 1.0)
    visited = []

    async def fake_enrich(movie_id, *, force=False, write_queue=None):
        visited.append(movie_id)
        return True

    monkeypatch.setattr(vod_importer, "enrich_movie", fake_enrich)
    vod_importer._ENRICH_PROGRESS["running"] = False
    asyncio.run(vod_importer.bulk_enrich_all(concurrency=2))
    assert visited == [stale]
    assert vod_importer._ENRICH_PROGRESS["movies_total"] == 2 and vod_importer._ENRICH_PROGRESS["movies_done"] == 2
    assert vod_importer._ENRICH_PROGRESS["running"] is False


def test_force_visits_everything(db, monkeypatch):
    fresh = db.upsert_movie("Fresh", 2020)
    _stamp(db, fresh, time.time())
    visited = []

    async def fake_enrich(movie_id, *, force=False, write_queue=None):
        visited.append(movie_id)
        return True

    monkeypatch.setattr(vod_importer, "enrich_movie", fake_enrich)
    vod_importer._ENRICH_PROGRESS["running"] = False
    asyncio.run(vod_importer.bulk_enrich_all(concurrency=2, force=True))
    assert visited == [fresh]


def test_bulk_work_uses_the_private_pool_and_everything_else_does_not(db):
    import threading

    names = {}

    def which():
        return threading.current_thread().name

    async def run():
        names["plain"] = await vod_importer._to_thread(which)
        vod_importer._in_bulk_enrich.set(True)
        names["bulk"] = await vod_importer._to_thread(which)

    asyncio.run(run())
    assert names["bulk"].startswith("bulk-enrich")
    assert not names["plain"].startswith("bulk-enrich")


def test_bulk_enrich_all_runs_its_titles_on_the_private_pool(db, monkeypatch):
    import threading

    stale = db.upsert_movie("Stale", 2021)
    _stamp(db, stale, 1.0)
    seen = []

    async def fake_enrich(movie_id, *, force=False, write_queue=None):
        seen.append(await vod_importer._to_thread(lambda: threading.current_thread().name))
        return True

    monkeypatch.setattr(vod_importer, "enrich_movie", fake_enrich)
    vod_importer._ENRICH_PROGRESS["running"] = False
    asyncio.run(vod_importer.bulk_enrich_all(concurrency=2))
    assert seen and all(n.startswith("bulk-enrich") for n in seen)
