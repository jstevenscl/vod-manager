"""A provider's "Series Imported" count must include series from the bulk
import, not only series whose episodes enrichment has already fetched --
otherwise a fresh provider showed 0 / N until enrichment reached it."""

import vod_db


def test_series_count_counts_imported_series_without_episodes(db):
    p = db.upsert_provider("prov1", "http://example.com", "u", "p")
    db.bulk_import_series(p, [
        {"name": "Show A", "year": 2020, "provider_series_id": "1", "raw_name": "Show A"},
        {"name": "Show B", "year": 2021, "provider_series_id": "2", "raw_name": "Show B"},
    ])
    vod_db._PROVIDER_COUNTS_CACHE["counts"] = None
    conn = vod_db._connect()
    try:
        counts = vod_db._provider_counts(conn)
    finally:
        conn.close()
    assert counts[p]["series_count"] == 2
    assert counts[p]["episode_count"] == 0
