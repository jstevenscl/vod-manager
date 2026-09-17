"""Regression coverage for queued/serialized catalog imports and shared
sidebar lifecycle status (ported from knmplace/vod-manager's
test_import_responsiveness.py, scoped to what this branch actually ports --
see vod_importer.import_provider_catalog's docstring. Not yet ported here:
the CPU-bound catalog-normalization extraction (_build_movie_import_items/
_build_series_import_items run via asyncio.to_thread) and the
catalog_fingerprint unchanged-source-skip optimization -- both filed as a
separate follow-up rather than rushed through in this pass."""

import asyncio

import vod_importer


def test_xc_imports_are_serialized(monkeypatch):
    active = 0
    peak = 0

    async def fake_impl(_provider_id):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return {"ok": True}

    monkeypatch.setattr(vod_importer.vod_db, "get_provider", lambda provider_id: {"id": provider_id, "name": str(provider_id)})
    monkeypatch.setattr(vod_importer, "_import_provider_catalog_impl", fake_impl)
    monkeypatch.setattr(vod_importer, "schedule_known_series_identity_reconciliation", lambda: False)

    async def run():
        await asyncio.gather(
            vod_importer.import_provider_catalog(1, schedule_enrichment=False),
            vod_importer.import_provider_catalog(2, schedule_enrichment=False),
        )

    asyncio.run(run())
    assert peak == 1


def test_non_xc_import_lifecycle_updates_shared_sidebar_status():
    previous = vod_importer.get_import_progress()
    try:
        vod_importer.mark_import_queued(42, "Plex", 1)
        queued = vod_importer.get_import_progress()
        assert queued["queued"] is True
        assert queued["running"] is False
        assert queued["provider_name"] == "Plex"

        vod_importer.mark_import_running(42, "Plex")
        running = vod_importer.get_import_progress()
        assert running["running"] is True
        assert running["queued"] is False
        assert running["provider_name"] == "Plex"

        vod_importer.mark_import_finished(42)
        finished = vod_importer.get_import_progress()
        assert finished["running"] is False
        assert finished["queued"] is False
        assert finished["error"] is None
    finally:
        vod_importer._IMPORT_PROGRESS.clear()
        vod_importer._IMPORT_PROGRESS.update(previous)


def test_mark_import_finished_ignores_a_superseded_provider():
    """The queue worker may have already moved on to the next provider by
    the time an earlier one's own cleanup runs -- mark_import_finished must
    not clobber that newer job's live state."""
    previous = vod_importer.get_import_progress()
    try:
        vod_importer.mark_import_running(1, "First")
        vod_importer.mark_import_running(2, "Second")
        vod_importer.mark_import_finished(1)
        still_second = vod_importer.get_import_progress()
        assert still_second["provider_id"] == 2
        assert still_second["running"] is True
    finally:
        vod_importer._IMPORT_PROGRESS.clear()
        vod_importer._IMPORT_PROGRESS.update(previous)


def test_evaluate_smart_category_scoped_ids_only_matches_given_pool(db, monkeypatch):
    movie_a = db.upsert_movie("Alpha", 2020)
    movie_b = db.upsert_movie("Beta", 2020)
    category_id = db.upsert_category("All Movies", "movie", is_smart=True, rule_json='{"match_all": true, "exclude_adult": false}')

    result_scoped = db.evaluate_smart_category(category_id, {movie_a})
    assert result_scoped["evaluated"] == 1
    assert result_scoped["matched"] == 1

    placements = db.list_movie_placements(movie_b)
    assert placements == []

    result_full = db.evaluate_smart_category(category_id)
    assert result_full["evaluated"] == 2


def test_evaluate_smart_category_empty_scoped_ids_is_a_no_op(db):
    db.upsert_movie("Alpha", 2020)
    category_id = db.upsert_category("All Movies", "movie", is_smart=True, rule_json='{"match_all": true, "exclude_adult": false}')

    result = db.evaluate_smart_category(category_id, set())
    assert result == {"evaluated": 0, "matched": 0, "newly_placed": 0}
