"""Failed episode-list fetches go to the back of the trickle queue.

A source whose get_series_info call failed used to keep its place at the
front (the queue is ordered by source id), so every batch re-hit the same
dead shows first. Now failed sources are tried only after every clean one,
and at most once per cooldown, so a provider that keeps failing for a show
no longer eats each batch. Opening a show still fetches it right away."""

import asyncio
import time

import vod_importer


def _seed(db, count):
    provider_id = db.upsert_provider("XC-Test", "http://xc.example.com", "user", "pass", provider_type="xc")
    db.bulk_import_series(provider_id, [{
        "name": f"Show {i}", "year": 2020, "provider_series_id": str(i),
        "provider_category_name": None, "raw_name": f"Show {i}", "_has_detail": True,
        "genre": None, "description": None, "cast_list": None, "director": None,
        "poster_url": None, "rating": None, "release_date": None, "tmdb_id": None,
        "provider_last_modified": None,
    } for i in range(count)])
    rows = db.list_pending_series_sources(provider_id)
    return provider_id, rows


def _fail(db, row, provider_series_id, age_seconds):
    db.record_series_source_failure(row["series_id"], row["provider_id"], provider_series_id)
    conn = db._connect()
    conn.execute("UPDATE series_sources SET last_failed_at=? WHERE id=?", (str(time.time() - age_seconds), row["id"]))
    conn.commit()
    conn.close()


def _psid(db, row):
    conn = db._connect()
    value = conn.execute("SELECT provider_series_id FROM series_sources WHERE id=?", (row["id"],)).fetchone()[0]
    conn.close()
    return value


def test_failed_sources_are_queued_after_clean_ones(db):
    provider_id, rows = _seed(db, 4)
    _fail(db, rows[0], _psid(db, rows[0]), age_seconds=2 * 86400)

    ordered = [r["id"] for r in db.list_pending_series_sources(provider_id)]

    assert ordered == [rows[1]["id"], rows[2]["id"], rows[3]["id"], rows[0]["id"]]


def test_recently_failed_source_waits_out_cooldown(db):
    provider_id, rows = _seed(db, 3)
    _fail(db, rows[0], _psid(db, rows[0]), age_seconds=60)

    ids = [r["id"] for r in db.list_pending_series_sources(provider_id, limit=10)]

    assert rows[0]["id"] not in ids
    assert ids == [rows[1]["id"], rows[2]["id"]]


def test_explicit_series_request_ignores_cooldown(db):
    provider_id, rows = _seed(db, 2)
    _fail(db, rows[0], _psid(db, rows[0]), age_seconds=60)

    ids = [r["id"] for r in db.list_pending_series_sources(provider_id, series_ids={rows[0]["series_id"]})]

    assert ids == [rows[0]["id"]]


def test_provider_fetch_failure_counts_toward_trickle_stop(monkeypatch):
    providers = [{"id": 1, "name": "Provider One", "is_active": True}]
    pending = [{"id": 100 + i, "series_id": 10 + i, "provider_id": 1} for i in range(10)]
    calls: list[int] = []

    async def no_sleep(seconds):
        pass

    async def provider_fails(series_id, provider_id, **kwargs):
        # The real per-source path records the failure and returns a result
        # instead of raising.
        calls.append(kwargs["source_id"])
        return {"fetched": False, "reason": "get_series_info failed for provider Provider One", "failed": True}

    monkeypatch.setattr(vod_importer.vod_db, "list_providers", lambda: providers)
    monkeypatch.setattr(vod_importer.vod_db, "list_pending_series_sources",
                        lambda provider_id, limit=None, series_ids=None: pending[:limit])
    monkeypatch.setattr(vod_importer.vod_db, "get_series_episode_summaries", lambda ids: {})
    monkeypatch.setattr(vod_importer.vod_db, "auto_merge_series_by_tmdb_batch", lambda ids: [])
    monkeypatch.setattr(vod_importer.vod_db, "auto_merge_series_tmdb_collisions", lambda ids=None: [])
    monkeypatch.setattr(vod_importer, "_trickle_sleep", no_sleep)
    monkeypatch.setattr(vod_importer, "_provider_backoff_remaining", lambda provider_id: 0.0)
    monkeypatch.setitem(vod_importer._ENRICH_PROGRESS, "running", False)
    monkeypatch.setattr(vod_importer, "enrich_series_source_only", provider_fails)

    asyncio.run(vod_importer.run_episode_trickle_tick(batch_size=10, spacing_seconds=0))

    assert len(calls) == 3
