"""beads-974 (Step 3, movies): retroactively re-split existing movie rows
whose sources span more than one language, to match the new (tmdb_id,
language) merge rule added in Step 1/2 -- these rows were merged back when
tmdb_id alone was the gate, before this bead's fix existed.

For every movie whose source-language groups (see _source_languages /
_split_by_language_conflict) don't all overlap, the LARGEST language group
stays on the original movie_id (keeps its poster/description/etc. as-is,
same "most-sourced stays put" convention as the rest of the merge/dedup
code), and each other group is split off into a brand-new movie row that's
a full copy of the original's metadata (per user direction 2026-09-10) with
only that group's movie_sources (and matching category placements) moved
over. Movies with sources in only one language group are left untouched."""

import config


def _import_movie(db, provider_id, name, stream_id, raw_name, tmdb_id, year=2001):
    db.bulk_import_movies(provider_id, [
        {
            "name": name,
            "year": year,
            "provider_stream_id": stream_id,
            "container_extension": "mp4",
            "raw_name": raw_name,
            "tmdb_id": tmdb_id,
            "_has_detail": True,
        },
    ])


def _make_mixed_language_movie(db, provider_id):
    """Simulates a movie row that was auto-merged pre-Step-1: one movies row
    carrying two movie_sources rows in different languages, which could only
    happen before the Step 1 gate existed (or via direct DB manipulation, as
    here, to reproduce that legacy shape without needing to disable the gate)."""
    _import_movie(db, provider_id, "Amelie", "en-1", "EN - Amelie", tmdb_id=194)
    movie = db.get_movie_by_name_year("Amelie", 2001)

    conn = db._connect()
    conn.execute(
        "INSERT INTO movie_sources (movie_id, provider_id, provider_stream_id, container_extension, "
        "raw_name, language, added_at, last_seen_at) VALUES (?,?,?,?,?,?,?,?)",
        (movie["id"], provider_id, "fr-1", "mp4", "FR - Amelie", "FR", "0", "0"),
    )
    conn.commit()
    conn.close()
    return movie


def test_dry_run_report_finds_mixed_language_movie(db):
    provider_id = db.upsert_provider("prov1", "http://example.com", "user", "pass")
    movie = _make_mixed_language_movie(db, provider_id)

    report = db.movie_language_split_dry_run_report()

    matching = [r for r in report["movies"] if r["movie_id"] == movie["id"]]
    assert len(matching) == 1
    assert matching[0]["language_groups"] == 2


def test_dry_run_report_ignores_single_language_movie(db):
    provider_id = db.upsert_provider("prov1", "http://example.com", "user", "pass")
    _import_movie(db, provider_id, "Amelie", "en-1", "EN - Amelie", tmdb_id=194)
    movie = db.get_movie_by_name_year("Amelie", 2001)

    report = db.movie_language_split_dry_run_report()

    assert all(r["movie_id"] != movie["id"] for r in report["movies"])


def test_apply_split_creates_new_row_for_minority_language(db):
    provider_id = db.upsert_provider("prov1", "http://example.com", "user", "pass")
    movie = _make_mixed_language_movie(db, provider_id)
    original_id = movie["id"]

    result = db.apply_movie_language_split()

    assert result["movies_split"] == 1
    assert result["new_rows_created"] == 1

    # Original row keeps the EN source (larger group).
    original_sources = db.list_movie_sources(original_id)
    assert {s["provider_stream_id"] for s in original_sources} == {"en-1"}

    # New row was created with the FR source, and a full metadata copy.
    all_movies = db.list_movies(limit=1000)
    new_rows = [m for m in all_movies if m["id"] != original_id and m["tmdb_id"] == "194"]
    assert len(new_rows) == 1
    new_movie = new_rows[0]
    assert new_movie["name"] == "Amelie"
    assert new_movie["year"] == 2001

    new_sources = db.list_movie_sources(new_movie["id"])
    assert {s["provider_stream_id"] for s in new_sources} == {"fr-1"}


def test_apply_split_moves_category_placement_to_new_row(db):
    config.save_enabled_languages(["EN", "ES", "FR"])
    provider_id = db.upsert_provider("prov1", "http://example.com", "user", "pass")
    movie = _make_mixed_language_movie(db, provider_id)
    category_id = db.upsert_category("Drama Movies", "movie")
    db.place_movie_in_category(movie["id"], category_id)

    db.apply_movie_language_split()

    all_movies = db.list_movies(limit=1000)
    new_movie = next(m for m in all_movies if m["id"] != movie["id"] and m["tmdb_id"] == "194")

    export_rows = db.get_movie_export_rows()
    placed_ids = {r["movie_id"] for r in export_rows if r["category_id"] == category_id}
    # Both the original (EN, kept category) and the new split-off (FR) row
    # should be independently placeable/placed -- the new row gets its own
    # placement copy so its FR sources remain browsable in that category.
    assert movie["id"] in placed_ids
    assert new_movie["id"] in placed_ids


def test_apply_split_is_idempotent(db):
    provider_id = db.upsert_provider("prov1", "http://example.com", "user", "pass")
    _make_mixed_language_movie(db, provider_id)

    first = db.apply_movie_language_split()
    second = db.apply_movie_language_split()

    assert first["movies_split"] == 1
    assert second["movies_split"] == 0
