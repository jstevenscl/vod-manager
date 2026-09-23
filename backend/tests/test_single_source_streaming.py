"""get_movie_source_for_streaming/get_episode_source_for_streaming back the
per-source "preview this specific provider's copy" button in the Sources
list. Found live 2026-09-23 testing real Dropbox/Drive playback: neither
query aliased its own row id as source_id, so xc_server's
record_source_success/_failure(kind, source["source_id"]) raised a bare
KeyError -- a 500 for EVERY provider type's per-source preview, not just the
one under test; nothing exercised this route before.
"""

import vod_db


def test_movie_single_source_has_source_id(db):
    pid = db.upsert_provider("p1", "http://x", "u", "p")
    mid = db.upsert_movie("Some Movie", 2000)
    db.add_movie_source(mid, pid, "s1", "mp4")
    row = vod_db.get_movie_source_for_streaming(
        vod_db._connect().execute("SELECT id FROM movie_sources WHERE movie_id=?", (mid,)).fetchone()[0]
    )
    assert row is not None and row["source_id"] is not None
    assert row["movie_id"] == mid


def test_episode_single_source_has_source_id(db):
    pid = db.upsert_provider("p1", "http://x", "u", "p")
    series_id = db.upsert_series("Some Show", 2010)
    episode_id = db.add_episode(series_id, 1, 1, "Pilot")
    db.add_episode_source(episode_id, pid, "e1", "mp4")
    row = vod_db.get_episode_source_for_streaming(
        vod_db._connect().execute("SELECT id FROM episode_sources WHERE episode_id=?", (episode_id,)).fetchone()[0]
    )
    assert row is not None and row["source_id"] is not None
    assert row["episode_id"] == episode_id


def test_single_source_id_matches_the_real_source_row(db):
    """Not just present -- must be the SAME id list_*_sources_for_streaming
    already uses, since xc_server treats both shapes interchangeably."""
    pid = db.upsert_provider("p1", "http://x", "u", "p")
    mid = db.upsert_movie("Some Movie", 2000)
    db.add_movie_source(mid, pid, "s1", "mp4")
    listed = vod_db.list_movie_sources_for_streaming(mid)[0]
    single = vod_db.get_movie_source_for_streaming(listed["source_id"])
    assert single["source_id"] == listed["source_id"]
