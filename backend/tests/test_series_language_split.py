"""beads-974 (Step 3, series): retroactively re-split existing series rows
(and their episodes) whose sources span more than one language, to match the
new (tmdb_id, language) merge rule added in Step 1/2 -- these rows were
merged back when tmdb_id alone was the gate, before this bead's fix existed.

Series are a harder case than movies because _merge_series_row can also
collapse two ALREADY-DISTINCT episode rows together whenever both series
carried the same (season_number, episode_number): the "from" episode's
episode_sources get reassigned onto the "into" episode's pre-existing row
and the "from" episode row itself is deleted (see _merge_series_row,
vod_db.py ~8163). So a single post-merge `episodes` row can carry
episode_sources spanning more than one language even when nothing else about
it looks unusual -- language lives entirely on episode_sources.language
(set independently per source, from that source's own raw_name -- see
_add_episode_source_row), never inherited from the parent series or episode
row. That means the split doesn't need to recover any lost pre-merge
lineage: grouping each episode's OWN episode_sources by language (the same
union-chaining rule as _group_source_languages_by_conflict) is sufficient on
its own, independent of how the series-level sources are grouped.

For every series whose series_sources span more than one language group, the
LARGEST group stays on the original series_id (metadata untouched, per the
"most-sourced stays put" convention already used for movies/Step2). Each
other group is split off into a brand-new series row that's a full metadata
copy of the original (per user direction 2026-09-10). Then, independently,
every episode under the original series has its own episode_sources grouped
by language; any source group that doesn't match the language now kept on
the original series gets moved to the matching new series's corresponding
(season_number, episode_number) episode row (created if needed)."""

import config


def _import_series(db, provider_id, name, series_stream_id, raw_name, tmdb_id, year=2017):
    db.bulk_import_series(provider_id, [
        {
            "name": name,
            "year": year,
            "provider_series_id": series_stream_id,
            "raw_name": raw_name,
            "tmdb_id": tmdb_id,
            "_has_detail": True,
        },
    ])


def _make_mixed_language_series(db, provider_id):
    """Simulates a series row that was auto-merged pre-Step-1: one series row
    carrying two series_sources rows in different languages, plus one episode
    whose episode_sources also span both languages (the _merge_series_row
    episode-collapse case) -- reproduced via direct DB manipulation, same
    approach as test_movie_language_split.py's _make_mixed_language_movie."""
    _import_series(db, provider_id, "Dark", "en-s1", "EN - Dark", tmdb_id=77)
    series = db.get_series_by_name_year("Dark", 2017)

    conn = db._connect()
    conn.execute(
        "INSERT INTO series_sources (series_id, provider_id, provider_series_id, raw_name, language, "
        "added_at, last_seen_at) VALUES (?,?,?,?,?,?,?)",
        (series["id"], provider_id, "de-s1", "DE - Dark", "DE", "0", "0"),
    )
    conn.commit()
    conn.close()

    episode_id = db.add_episode(series["id"], 1, 1, "Secrets")
    conn = db._connect()
    conn.execute(
        "INSERT INTO episode_sources (episode_id, provider_id, provider_stream_id, container_extension, "
        "raw_name, language, added_at, last_seen_at) VALUES (?,?,?,?,?,?,?,?)",
        (episode_id, provider_id, "en-e1", "mp4", "EN - Dark S01E01", "EN", "0", "0"),
    )
    conn.execute(
        "INSERT INTO episode_sources (episode_id, provider_id, provider_stream_id, container_extension, "
        "raw_name, language, added_at, last_seen_at) VALUES (?,?,?,?,?,?,?,?)",
        (episode_id, provider_id, "de-e1", "mp4", "DE - Dark S01E01", "DE", "0", "0"),
    )
    conn.commit()
    conn.close()
    return series, episode_id


def test_dry_run_report_finds_mixed_language_series(db):
    provider_id = db.upsert_provider("prov1", "http://example.com", "user", "pass")
    series, _ = _make_mixed_language_series(db, provider_id)

    report = db.series_language_split_dry_run_report()

    matching = [r for r in report["series"] if r["series_id"] == series["id"]]
    assert len(matching) == 1
    assert matching[0]["language_groups"] == 2


def test_dry_run_report_ignores_single_language_series(db):
    provider_id = db.upsert_provider("prov1", "http://example.com", "user", "pass")
    _import_series(db, provider_id, "Dark", "en-s1", "EN - Dark", tmdb_id=77)
    series = db.get_series_by_name_year("Dark", 2017)

    report = db.series_language_split_dry_run_report()

    assert all(r["series_id"] != series["id"] for r in report["series"])


def test_apply_split_creates_new_series_row_for_minority_language(db):
    provider_id = db.upsert_provider("prov1", "http://example.com", "user", "pass")
    series, episode_id = _make_mixed_language_series(db, provider_id)
    original_id = series["id"]

    result = db.apply_series_language_split()

    assert result["series_split"] == 1
    assert result["new_rows_created"] == 1

    original_sources = db.list_series_sources(original_id)
    assert {s["provider_series_id"] for s in original_sources} == {"en-s1"}

    all_series = db.list_series(limit=1000)
    new_rows = [s for s in all_series if s["id"] != original_id and s["tmdb_id"] == "77"]
    assert len(new_rows) == 1
    new_series = new_rows[0]
    assert new_series["name"] == "Dark"
    assert new_series["year"] == 2017

    new_sources = db.list_series_sources(new_series["id"])
    assert {s["provider_series_id"] for s in new_sources} == {"de-s1"}


def test_apply_split_moves_episode_sources_to_matching_language_series(db):
    provider_id = db.upsert_provider("prov1", "http://example.com", "user", "pass")
    series, episode_id = _make_mixed_language_series(db, provider_id)
    original_id = series["id"]

    db.apply_series_language_split()

    all_series = db.list_series(limit=1000)
    new_series = next(s for s in all_series if s["id"] != original_id and s["tmdb_id"] == "77")

    # Original episode row keeps only the EN source.
    original_episodes = db.list_episodes(original_id)
    assert len(original_episodes) == 1
    original_ep_id = original_episodes[0]["id"]
    original_ep_sources = db.list_episode_sources_for_episode_ids([original_ep_id])[original_ep_id]
    assert {s["provider_stream_id"] for s in original_ep_sources} == {"en-e1"}

    # New series got its own S01E01 episode row carrying the DE source.
    new_episodes = db.list_episodes(new_series["id"])
    assert len(new_episodes) == 1
    assert new_episodes[0]["season_number"] == 1
    assert new_episodes[0]["episode_number"] == 1
    new_ep_id = new_episodes[0]["id"]
    new_ep_sources = db.list_episode_sources_for_episode_ids([new_ep_id])[new_ep_id]
    assert {s["provider_stream_id"] for s in new_ep_sources} == {"de-e1"}


def test_apply_split_moves_category_placement_to_new_series_row(db):
    config.save_enabled_languages(["EN", "ES", "DE"])
    provider_id = db.upsert_provider("prov1", "http://example.com", "user", "pass")
    series, _ = _make_mixed_language_series(db, provider_id)
    category_id = db.upsert_category("Drama Shows", "series")
    db.place_series_in_category(series["id"], category_id)

    db.apply_series_language_split()

    all_series = db.list_series(limit=1000)
    new_series = next(s for s in all_series if s["id"] != series["id"] and s["tmdb_id"] == "77")

    placements_original = db.list_series_sources(series["id"])  # sanity: still exists
    assert placements_original

    # Both original (EN) and split-off (DE) series should each carry their
    # own category placement so both remain independently browsable.
    conn = db._connect()
    placed_ids = {
        row["series_id"]
        for row in conn.execute(
            "SELECT series_id FROM series_category_placements WHERE category_id=?", (category_id,)
        ).fetchall()
    }
    conn.close()
    assert series["id"] in placed_ids
    assert new_series["id"] in placed_ids


def test_apply_split_is_idempotent(db):
    provider_id = db.upsert_provider("prov1", "http://example.com", "user", "pass")
    _make_mixed_language_series(db, provider_id)

    first = db.apply_series_language_split()
    second = db.apply_series_language_split()

    assert first["series_split"] == 1
    assert second["series_split"] == 0


def test_movies_and_series_split_together(db):
    """User's Step 2 instruction ('run same tests again include movies and
    shows together') applied to Step 3 as well -- both apply functions must
    coexist cleanly against the same db/session."""
    import tests.test_movie_language_split as movie_mod

    provider_id = db.upsert_provider("prov1", "http://example.com", "user", "pass")
    movie = movie_mod._make_mixed_language_movie(db, provider_id)
    series, _ = _make_mixed_language_series(db, provider_id)

    movie_result = db.apply_movie_language_split()
    series_result = db.apply_series_language_split()

    assert movie_result["movies_split"] == 1
    assert series_result["series_split"] == 1
