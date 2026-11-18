"""Sync History report (phase timings, per-run card events, delete) and the
Episode discovery progress/cancel state shown on Curation & Maintenance.

Report layout and progress/cancel design: @knmplace (PR #35 follow-ups)."""

import asyncio
import sqlite3

import vod_db
import vod_importer


def _movie_item(stream_id, name="New Movie", year=2024):
    return {"name": name, "year": year, "provider_stream_id": stream_id,
            "container_extension": "mp4", "raw_name": name}


def test_report_snapshots_changed_cards_and_deletes_only_the_report(db):
    movie_id = db.upsert_movie("New Movie", 2024, tmdb_id="100")
    series_id = db.upsert_series("New Series", 2024, tmdb_id="200")
    run_id = db.create_catalog_sync_run(7, "Provider 7", "xc")

    inserted = db.record_catalog_sync_events(
        run_id, movie_ids=[movie_id], series_ids=[series_id],
        created_movie_ids=[movie_id], created_series_ids=[],
        summary={"movies_created": 1},
    )
    assert inserted == 2
    db.finish_catalog_sync_run(run_id, summary={"movies_created": 1})

    [report] = db.list_catalog_sync_runs()
    assert report["id"] == run_id and report["event_count"] == 2
    detail = db.get_catalog_sync_run(run_id)
    actions = {(e["content_type"], e["action"]) for e in detail["events"]}
    assert actions == {("movie", "added"), ("series", "changed")}

    assert db.delete_catalog_sync_runs([run_id, run_id]) == 1
    assert db.get_catalog_sync_run(run_id) is None
    assert db.delete_catalog_sync_runs([run_id]) == 0
    assert db.delete_catalog_sync_runs([]) == 0
    with sqlite3.connect(vod_db.DB_PATH) as conn:
        assert conn.execute("SELECT COUNT(*) FROM catalog_sync_events").fetchone()[0] == 0
    # Catalog content is untouched.
    assert db.get_movie(movie_id)["name"] == "New Movie"
    assert db.get_series(series_id)["name"] == "New Series"


def test_phase_timings_are_stamped_and_fall_back_for_old_rows(db):
    run_id = db.create_catalog_sync_run(None, "Library", "plex")
    running = db.get_catalog_sync_run(run_id)
    assert running["import_started_at"] and running["import_finished_at"] is None
    assert running["run_type"] == "plex"

    db.finish_catalog_sync_run(run_id)
    done = db.get_catalog_sync_run(run_id)
    assert done["import_finished_at"] and done["ready_at"]

    db.update_catalog_sync_run(run_id, enrichment_started_at="1", enrichment_finished_at="2", status="ignored")
    assert db.get_catalog_sync_run(run_id)["enrichment_finished_at"] == "2"
    assert db.get_catalog_sync_run(run_id)["status"] == "ready"  # only timing fields are writable

    # A pre-existing row (phase columns NULL) reads back through started/finished.
    with sqlite3.connect(vod_db.DB_PATH) as conn:
        conn.execute(
            """INSERT INTO catalog_sync_runs (provider_name, provider_type, queued_at, started_at,
                   finished_at, status, created_at) VALUES ('Old', 'xc', '1', '10', '25', 'ready', '1')"""
        )
    old = next(r for r in db.list_catalog_sync_runs() if r["provider_name"] == "Old")
    assert (old["import_started_at"], old["import_finished_at"], old["ready_at"]) == ("10", "25", "25")


def test_init_db_adds_phase_columns_to_an_existing_database(tmp_path, monkeypatch):
    path = tmp_path / "old.sqlite"
    with sqlite3.connect(path) as conn:  # the pre-feature table shape
        conn.execute(
            """CREATE TABLE catalog_sync_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, provider_id INTEGER,
               provider_name TEXT NOT NULL, provider_type TEXT NOT NULL, queued_at TEXT NOT NULL,
               started_at TEXT, finished_at TEXT, status TEXT NOT NULL DEFAULT 'running',
               summary_json TEXT NOT NULL DEFAULT '{}', error TEXT, created_at TEXT NOT NULL)"""
        )
        conn.execute(
            "INSERT INTO catalog_sync_runs (provider_name, provider_type, queued_at, started_at, finished_at,"
            " status, created_at) VALUES ('Legacy', 'xc', '1', '5', '9', 'ready', '1')"
        )
    monkeypatch.setattr(vod_db, "DB_PATH", path)
    vod_db.init_db()
    vod_db.init_db()  # idempotent
    with sqlite3.connect(path) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(catalog_sync_runs)")}
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"import_started_at", "import_finished_at", "reconciliation_started_at",
            "reconciliation_finished_at", "enrichment_started_at", "enrichment_finished_at", "ready_at"} <= cols
    assert "catalog_sync_events" in tables
    [row] = vod_db.list_catalog_sync_runs()
    assert row["provider_name"] == "Legacy" and row["import_started_at"] == "5"


def test_bulk_import_reports_created_and_changed_cards(db):
    provider_id = db.upsert_provider("P", "http://p.invalid", "u", "p")
    first = db.bulk_import_movies(provider_id, [_movie_item("1")])
    assert first["movies_created"] == 1
    assert len(first["created_movie_ids"]) == 1 and first["changed_movie_ids"] == first["created_movie_ids"]

    # Same stream again: nothing new to report.
    again = db.bulk_import_movies(provider_id, [_movie_item("1")])
    assert again["created_movie_ids"] == [] and again["changed_movie_ids"] == []

    # A second source for the same title is a change, not a new card.
    other = db.upsert_provider("Q", "http://q.invalid", "u", "p")
    extra = db.bulk_import_movies(other, [_movie_item("9")])
    assert extra["created_movie_ids"] == []
    assert extra["changed_movie_ids"] == first["created_movie_ids"]


def _trickle_env(monkeypatch, n=5):
    providers = [{"id": 1, "name": "P1", "provider_type": "xc", "is_active": True}]
    pending = [{"id": 10 + i, "series_id": i, "provider_id": 1} for i in range(n)]

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(vod_importer.vod_db, "list_providers", lambda: providers)
    monkeypatch.setattr(vod_importer.vod_db, "list_pending_series_sources",
                        lambda provider_id, limit=None: pending[:limit] if limit else pending)
    monkeypatch.setattr(vod_importer, "_trickle_sleep", no_sleep)
    monkeypatch.setattr(vod_importer, "_provider_backoff_remaining", lambda provider_id: 0)
    monkeypatch.setattr(vod_importer, "_ENRICH_PROGRESS", {"running": False})
    monkeypatch.setattr(vod_importer, "_EPISODE_TRICKLE_RUNNING", False)


def test_episode_discovery_progress_counts_sources_and_errors(monkeypatch):
    _trickle_env(monkeypatch, n=4)
    outcomes = iter([True, False, True, True])

    async def enrich(series_id, provider_id, **kwargs):
        return {"fetched": next(outcomes)}

    monkeypatch.setattr(vod_importer, "enrich_series_source_only", enrich)
    assert vod_importer.cancel_episode_discovery() is False  # nothing running yet
    asyncio.run(vod_importer.run_episode_trickle_tick(batch_size=10, spacing_seconds=0))

    progress = vod_importer.get_episode_discovery_progress()
    assert progress["running"] is False and progress["cancelled"] is False
    assert (progress["series_sources_total"], progress["series_sources_done"], progress["series_errors"]) == (4, 4, 1)
    assert progress["finished_at"] is not None


def test_episode_discovery_can_be_cancelled_between_sources(monkeypatch):
    _trickle_env(monkeypatch, n=5)
    seen = []

    async def enrich(series_id, provider_id, **kwargs):
        seen.append(series_id)
        assert vod_importer.get_episode_discovery_progress()["running"] is True
        if len(seen) == 2:
            assert vod_importer.cancel_episode_discovery() is True
        return {"fetched": True}

    monkeypatch.setattr(vod_importer, "enrich_series_source_only", enrich)
    asyncio.run(vod_importer.run_episode_trickle_tick(batch_size=10, spacing_seconds=0))

    progress = vod_importer.get_episode_discovery_progress()
    assert seen == [0, 1]
    assert progress["cancelled"] is True and progress["running"] is False
    assert progress["series_sources_done"] == 2
