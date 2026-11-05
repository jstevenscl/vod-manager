import pytest

import config


def _movie_source(db, title: str, provider_id: int, stream_id: str) -> tuple[int, int]:
    movie_id = db.upsert_movie(title, 2026)
    db.add_movie_source(movie_id, provider_id, stream_id, "mkv")
    conn = db._connect()
    try:
        source_id = conn.execute("SELECT id FROM movie_sources WHERE movie_id=?", (movie_id,)).fetchone()["id"]
    finally:
        conn.close()
    return movie_id, source_id


@pytest.mark.parametrize("observed", [["DE"], ["DE", "SV"], ["DE", "FR", "SV"]])
def test_all_unwanted_observed_languages_propose_parent_archive(db, monkeypatch, observed):
    monkeypatch.setattr(config, "CONFIG_FILE", config.CONFIG_FILE.parent / "language-choice.json")
    config.save_enabled_languages(["EN"])
    provider_id = db.upsert_provider("language-choice-provider", "", "", "", provider_type="library")
    movie_id, source_id = _movie_source(db, "Ambiguous Audio", provider_id, "ambiguous")
    conn = db._connect()
    try:
        conn.execute("UPDATE movie_sources SET language='EN' WHERE id=?", (source_id,))
        conn.commit()
    finally:
        conn.close()
    db.set_source_language_probe("movie", source_id, observed, "observed", "local_embedded_audio_tags", "high")

    row = next(item for item in db.list_language_mismatches()["results"] if item["source_id"] == source_id)
    assert row["safe_to_apply"] is True
    assert row["proposed_archive"] is True
    result = db.decide_language_mismatch("movie", source_id, "approved")
    assert result["action"] == "parent_archived"
    assert result["language"] is None
    assert db.get_language_probe_source("movie", source_id)["language"] == "EN"
    assert db.get_language_probe_source("movie", source_id)["language_probe_decision"] == "approved"
    assert db.get_movie(movie_id)["review_excluded"] == 1


def test_mixed_wanted_and_unwanted_observations_still_require_language_choice(db, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_FILE", config.CONFIG_FILE.parent / "language-choice-mixed.json")
    config.save_enabled_languages(["EN"])
    provider_id = db.upsert_provider("language-choice-mixed-provider", "", "", "", provider_type="library")
    _, source_id = _movie_source(db, "Mixed Audio", provider_id, "mixed")
    conn = db._connect()
    try:
        conn.execute("UPDATE movie_sources SET language='ES' WHERE id=?", (source_id,))
        conn.commit()
    finally:
        conn.close()
    db.set_source_language_probe("movie", source_id, ["DE", "EN"], "observed", "local_embedded_audio_tags", "high")

    row = next(item for item in db.list_language_mismatches()["results"] if item["source_id"] == source_id)
    assert row["safe_to_apply"] is False
    assert row["proposed_archive"] is False
    with pytest.raises(ValueError, match="choose one of the observed languages"):
        db.decide_language_mismatch("movie", source_id, "approved")


def test_unknown_language_bulk_approval_updates_movies_episodes_and_audit_atomically(db, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_FILE", config.CONFIG_FILE.parent / "bulk-language.json")
    config.save_enabled_languages(["EN", "ES"])
    provider_id = db.upsert_provider("bulk-language-provider", "", "", "", provider_type="library")
    _, source_a = _movie_source(db, "Unknown Movie A", provider_id, "unknown-a")
    _, source_b = _movie_source(db, "Unknown Movie B", provider_id, "unknown-b")
    series_id = db.upsert_series("Unknown Series", 2026)
    episode_id = db.add_episode(series_id, 1, 1, "Episode")
    episode_source = db.add_episode_source(episode_id, provider_id, "unknown-episode", "mkv")
    for kind, source_id in (("movie", source_a), ("movie", source_b), ("episode", episode_source)):
        db.set_source_language_probe(kind, source_id, ["UNKNOWN"], "observed", "local_embedded_audio_tags", "low")

    payload = [
        {"kind": "movie", "source_id": source_a},
        {"kind": "movie", "source_id": source_b},
        {"kind": "episode", "source_id": episode_source},
    ]
    with pytest.raises(ValueError, match="no longer exist"):
        db.approve_language_probe_sources([*payload, {"kind": "movie", "source_id": 999999}], "EN")
    assert db.get_language_probe_source("movie", source_a)["language_probe_decision"] is None

    result = db.approve_language_probe_sources(payload, "EN")
    assert result == {"action": "approved", "language": "EN", "count": 3}
    assert db.get_language_probe_source("movie", source_a)["language"] == "EN"
    assert db.get_language_probe_source("movie", source_b)["language"] == "EN"
    assert db.get_language_probe_source("episode", episode_source)["language"] == "EN"
    assert db.list_language_unknowns()["results"] == []
    conn = db._connect()
    try:
        runs = conn.execute("SELECT COUNT(*) FROM language_probe_runs WHERE status='manual_approved'").fetchone()[0]
    finally:
        conn.close()
    assert runs == 3


def test_unknown_language_bulk_archive_keeps_sources_and_removes_all_placements(db):
    provider_id = db.upsert_provider("bulk-archive-provider", "", "", "", provider_type="library")
    movie_id, movie_source = _movie_source(db, "Archive Movie", provider_id, "archive-movie")
    series_id = db.upsert_series("Archive Series", 2026)
    episode_id = db.add_episode(series_id, 1, 1, "Episode")
    episode_source = db.add_episode_source(episode_id, provider_id, "archive-episode", "mkv")
    movie_category = db.upsert_category("Archive Movies", "movie")
    series_category = db.upsert_category("Archive Series", "series")
    db.bulk_place_movies_in_category([movie_id], movie_category)
    db.bulk_place_series_in_category([series_id], series_category)

    result = db.bulk_archive_language_probe_parents([
        {"kind": "movie", "item_id": movie_id},
        {"kind": "episode", "item_id": series_id},
        {"kind": "episode", "item_id": series_id},
    ])

    assert result == {"action": "archived", "count": 2}
    assert db.get_movie(movie_id)["review_excluded_manual"] == 1
    assert db.get_series(series_id)["review_excluded_manual"] == 1
    assert db.get_language_probe_source("movie", movie_source)
    assert db.get_language_probe_source("episode", episode_source)
    conn = db._connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM movie_category_placements WHERE movie_id=?", (movie_id,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM series_category_placements WHERE series_id=?", (series_id,)).fetchone()[0] == 0
    finally:
        conn.close()
