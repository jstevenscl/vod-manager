"""beads-974: auto-merge must gate on (tmdb_id, language), not tmdb_id alone,
so provider language/dub variants of the same title no longer collapse into
one card that can silently fail over to an unwanted-language stream. Movies
and series have no language column of their own -- language lives on their
_sources rows -- so the gate compares each candidate's source languages
(COALESCE'd to EN, matching _source_language's own untagged default).

Distinct movie/series `name` values are used per variant below (mirroring
real provider feeds, where a language variant typically carries a distinct
prefixed name like "FR - Amelie") -- bulk_import_movies/bulk_import_series
match existing rows by (name, year) first, so two variants sharing the exact
same name+year would collapse into ONE row/source pre-merge, never reaching
auto_merge_* as two separate rows at all."""

import config


def _import_movie(db, provider_id, name, year, stream_id, raw_name, tmdb_id):
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
    return db.get_movie_by_name_year(name, year)


def test_auto_merge_movie_skips_same_tmdb_id_different_language(db):
    config.save_duplicate_finder_auto_merge_tmdb(True)
    config.save_enabled_languages(["EN", "FR"])  # both sides enabled -> real mismatch
    provider_id = db.upsert_provider("prov1", "http://example.com", "user", "pass")

    en_movie = _import_movie(db, provider_id, "Amelie", 2001, "en-1", "EN - Amelie", tmdb_id=194)
    fr_movie = _import_movie(db, provider_id, "FR - Amelie", 2001, "fr-1", "FR - Amelie", tmdb_id=194)

    db.auto_merge_movie_by_tmdb(en_movie["id"])

    # Different languages under the same tmdb_id must stay as separate cards.
    assert db.get_movie(en_movie["id"]) is not None
    assert db.get_movie(fr_movie["id"]) is not None


def test_auto_merge_movie_merges_same_tmdb_id_same_language(db):
    config.save_duplicate_finder_auto_merge_tmdb(True)
    provider_id = db.upsert_provider("prov1", "http://example.com", "user", "pass")

    movie_a = _import_movie(db, provider_id, "Amelie", 2001, "en-1", "EN - Amelie", tmdb_id=194)
    movie_b = _import_movie(db, provider_id, "Amelie (Provider2)", 2001, "en-2", "EN - Amelie", tmdb_id=194)

    db.auto_merge_movie_by_tmdb(movie_a["id"])

    survivors = [mid for mid in (movie_a["id"], movie_b["id"]) if db.get_movie(mid)]
    assert len(survivors) == 1


def test_auto_merge_movie_merges_same_tmdb_id_when_both_untagged(db):
    config.save_duplicate_finder_auto_merge_tmdb(True)
    provider_id = db.upsert_provider("prov1", "http://example.com", "user", "pass")

    # No recognizable language prefix on either raw_name -- both default to
    # EN per _source_language, so this is the backward-compatible case.
    movie_a = _import_movie(db, provider_id, "Amelie", 2001, "p1-1", "Amelie", tmdb_id=194)
    movie_b = _import_movie(db, provider_id, "Amelie (Provider2)", 2001, "p1-2", "Amelie", tmdb_id=194)

    db.auto_merge_movie_by_tmdb(movie_a["id"])

    survivors = [mid for mid in (movie_a["id"], movie_b["id"]) if db.get_movie(mid)]
    assert len(survivors) == 1


def test_auto_merge_movie_merges_when_one_side_untagged(db):
    """User's rule: untagged sources default to EN, so an untagged variant
    still merges with an explicitly-EN-tagged variant of the same tmdb_id."""
    config.save_duplicate_finder_auto_merge_tmdb(True)
    provider_id = db.upsert_provider("prov1", "http://example.com", "user", "pass")

    movie_a = _import_movie(db, provider_id, "Amelie", 2001, "en-1", "EN - Amelie", tmdb_id=194)
    movie_b = _import_movie(db, provider_id, "Amelie (Provider2)", 2001, "p2-1", "Amelie", tmdb_id=194)

    db.auto_merge_movie_by_tmdb(movie_a["id"])

    survivors = [mid for mid in (movie_a["id"], movie_b["id"]) if db.get_movie(mid)]
    assert len(survivors) == 1


def test_auto_merge_series_skips_same_tmdb_id_different_language(db):
    config.save_duplicate_finder_auto_merge_tmdb(True)
    config.save_enabled_languages(["EN", "DE"])  # both sides enabled -> real mismatch
    provider_id = db.upsert_provider("prov1", "http://example.com", "user", "pass")

    db.bulk_import_series(provider_id, [
        {"name": "Dark", "year": 2017, "provider_series_id": "en-s1", "raw_name": "EN - Dark", "tmdb_id": 77, "_has_detail": True}
    ])
    db.bulk_import_series(provider_id, [
        {"name": "DE - Dark", "year": 2017, "provider_series_id": "de-s1", "raw_name": "DE - Dark", "tmdb_id": 77, "_has_detail": True}
    ])
    rows = [s for s in db.list_series(limit=1000) if "Dark" in s["name"]]
    assert len(rows) == 2

    db.auto_merge_series_by_tmdb(rows[0]["id"])

    remaining = [s for s in db.list_series(limit=1000) if "Dark" in s["name"]]
    assert len(remaining) == 2


def test_auto_merge_movie_skips_when_other_side_language_is_disabled(db):
    """Regression (2026-09-13, user report): the merge gate briefly (2026-09-12
    commit 2c681a3) compared source languages filtered down to
    config.get_enabled_languages(), so that a real, actually-spoken language
    that just isn't currently enabled for playback (e.g. ES with only EN
    enabled) read as an empty set and silently disabled the language-overlap
    check altogether -- letting an EN card and its ES sibling merge every
    enrichment cycle even though they share no language at all. This
    recreated exactly what the daily language-split maintenance tool exists
    to undo. "Enabled for playback" is a live, orthogonal, user-configurable
    query-time filter (config.get_enabled_languages) -- it says nothing about
    what language a source actually carries, so merge-safety must be judged
    on the unfiltered, real source languages regardless of the playback
    config, exactly like _shares_a_language (used at import time) already
    does."""
    config.save_duplicate_finder_auto_merge_tmdb(True)
    config.save_enabled_languages(["EN"])
    provider_id = db.upsert_provider("prov1", "http://example.com", "user", "pass")

    en_movie = _import_movie(db, provider_id, "Amelie", 2001, "en-1", "EN - Amelie", tmdb_id=194)
    es_movie = _import_movie(db, provider_id, "ES - Amelie", 2001, "es-1", "ES - Amelie", tmdb_id=194)

    db.auto_merge_movie_by_tmdb(en_movie["id"])

    # Different real languages must stay split even though ES isn't enabled.
    assert db.get_movie(en_movie["id"]) is not None
    assert db.get_movie(es_movie["id"]) is not None


def test_auto_merge_movie_still_skips_two_enabled_different_languages(db):
    """The enabled-languages relaxation must not swallow the original
    beads-974 protection: if BOTH sides' languages are enabled for
    playback, a real mismatch must still block the merge."""
    config.save_duplicate_finder_auto_merge_tmdb(True)
    config.save_enabled_languages(["EN", "FR"])
    provider_id = db.upsert_provider("prov1", "http://example.com", "user", "pass")

    en_movie = _import_movie(db, provider_id, "Amelie", 2001, "en-1", "EN - Amelie", tmdb_id=194)
    fr_movie = _import_movie(db, provider_id, "FR - Amelie", 2001, "fr-1", "FR - Amelie", tmdb_id=194)

    db.auto_merge_movie_by_tmdb(en_movie["id"])

    assert db.get_movie(en_movie["id"]) is not None
    assert db.get_movie(fr_movie["id"]) is not None


def test_auto_merge_series_skips_when_other_side_language_is_disabled(db):
    """Series twin of test_auto_merge_movie_skips_when_other_side_language_is_disabled."""
    config.save_duplicate_finder_auto_merge_tmdb(True)
    config.save_enabled_languages(["EN"])
    provider_id = db.upsert_provider("prov1", "http://example.com", "user", "pass")

    db.bulk_import_series(provider_id, [
        {"name": "Dark", "year": 2017, "provider_series_id": "en-s1", "raw_name": "EN - Dark", "tmdb_id": 77, "_has_detail": True}
    ])
    db.bulk_import_series(provider_id, [
        {"name": "ES - Dark", "year": 2017, "provider_series_id": "es-s1", "raw_name": "ES - Dark", "tmdb_id": 77, "_has_detail": True}
    ])
    rows = [s for s in db.list_series(limit=1000) if "Dark" in s["name"]]
    assert len(rows) == 2

    db.auto_merge_series_by_tmdb(rows[0]["id"])

    remaining = [s for s in db.list_series(limit=1000) if "Dark" in s["name"]]
    assert len(remaining) == 2


def test_auto_merge_series_merges_same_tmdb_id_same_language(db):
    config.save_duplicate_finder_auto_merge_tmdb(True)
    provider_id = db.upsert_provider("prov1", "http://example.com", "user", "pass")

    db.bulk_import_series(provider_id, [
        {"name": "Dark", "year": 2017, "provider_series_id": "en-s1", "raw_name": "EN - Dark", "tmdb_id": 77, "_has_detail": True}
    ])
    db.bulk_import_series(provider_id, [
        {"name": "Dark (Provider2)", "year": 2017, "provider_series_id": "en-s2", "raw_name": "EN - Dark", "tmdb_id": 77, "_has_detail": True}
    ])
    rows = [s for s in db.list_series(limit=1000) if "Dark" in s["name"]]
    assert len(rows) == 2

    db.auto_merge_series_by_tmdb(rows[0]["id"])

    remaining = [s for s in db.list_series(limit=1000) if "Dark" in s["name"]]
    assert len(remaining) == 1


def test_auto_merge_movies_by_tmdb_batch_merges_each_id_sequentially(db):
    """2026-09-14 CPU-spike fix: bulk enrich's end-of-run sweep used to fan
    out one asyncio.to_thread(auto_merge_movie_by_tmdb, id) task per affected
    id -- thousands of OS threads submitted at once against a full catalog,
    even though every merge is already serialized by _WRITE_LOCK. The batch
    helper must produce the identical merge outcome as calling
    auto_merge_movie_by_tmdb once per id in a loop."""
    config.save_duplicate_finder_auto_merge_tmdb(True)
    provider_id = db.upsert_provider("prov1", "http://example.com", "user", "pass")

    a = _import_movie(db, provider_id, "Movie A", 2001, "a-1", "Movie A", tmdb_id=501)
    a_dup = _import_movie(db, provider_id, "Movie A Dup", 2001, "a-2", "Movie A Dup", tmdb_id=501)
    b = _import_movie(db, provider_id, "Movie B", 2005, "b-1", "Movie B", tmdb_id=502)
    b_dup = _import_movie(db, provider_id, "Movie B Dup", 2005, "b-2", "Movie B Dup", tmdb_id=502)

    db.auto_merge_movies_by_tmdb_batch([a["id"], b["id"]])

    assert db.get_movie(a["id"]) is not None
    assert db.get_movie(a_dup["id"]) is None
    assert db.get_movie(b["id"]) is not None
    assert db.get_movie(b_dup["id"]) is None


def test_auto_merge_series_by_tmdb_batch_merges_each_id_sequentially(db):
    """Series counterpart to test_auto_merge_movies_by_tmdb_batch_merges_each_id_sequentially."""
    config.save_duplicate_finder_auto_merge_tmdb(True)
    provider_id = db.upsert_provider("prov1", "http://example.com", "user", "pass")

    db.bulk_import_series(provider_id, [
        {"name": "Show A", "year": 2010, "provider_series_id": "a-1", "raw_name": "Show A",
         "tmdb_id": 601, "_has_detail": True, "provider_category_name": None, "genre": None,
         "description": None, "cast_list": None, "director": None, "poster_url": None,
         "rating": None, "release_date": None, "provider_last_modified": None},
        {"name": "Show A Dup", "year": 2010, "provider_series_id": "a-2", "raw_name": "Show A Dup",
         "tmdb_id": 601, "_has_detail": True, "provider_category_name": None, "genre": None,
         "description": None, "cast_list": None, "director": None, "poster_url": None,
         "rating": None, "release_date": None, "provider_last_modified": None},
    ])
    rows = [s for s in db.list_series(limit=1000) if s["name"] in ("Show A", "Show A Dup")]
    assert len(rows) == 2

    db.auto_merge_series_by_tmdb_batch([rows[0]["id"]])

    remaining = [s for s in db.list_series(limit=1000) if s["name"] in ("Show A", "Show A Dup")]
    assert len(remaining) == 1


def test_collision_sweep_merges_exact_tmdb_series_omitted_from_work_list(db):
    """A final DB-derived sweep catches siblings missed by a coalesced run."""
    config.save_duplicate_finder_auto_merge_tmdb(True)
    provider_id = db.upsert_provider("prov1", "http://example.com", "user", "pass")
    db.bulk_import_series(provider_id, [
        {"name": "Canonical Show", "year": 2020, "provider_series_id": "canonical",
         "raw_name": "Canonical Show", "tmdb_id": 602, "_has_detail": True,
         "provider_category_name": None, "genre": None, "description": None, "cast_list": None,
         "director": None, "poster_url": None, "rating": None, "release_date": None,
         "provider_last_modified": None},
        {"name": "Provider Alias", "year": 2020, "provider_series_id": "alias",
         "raw_name": "Provider Alias", "tmdb_id": 602, "_has_detail": True,
         "provider_category_name": None, "genre": None, "description": None, "cast_list": None,
         "director": None, "poster_url": None, "rating": None, "release_date": None,
         "provider_last_modified": None},
    ])

    db.auto_merge_series_tmdb_collisions()

    remaining = [s for s in db.list_series(limit=1000) if s["tmdb_id"] == "602"]
    assert len(remaining) == 1


def test_collision_sweep_merges_exact_tmdb_movies_omitted_from_work_list(db):
    """Known-ID movies may skip detail enrichment but must still reconcile."""
    config.save_duplicate_finder_auto_merge_tmdb(True)
    provider_id = db.upsert_provider("prov1", "http://example.com", "user", "pass")
    first = _import_movie(db, provider_id, "Canonical Movie", 2020, "canonical", "Canonical Movie", tmdb_id=701)
    second = _import_movie(db, provider_id, "Provider Movie Alias", 2020, "alias", "Provider Movie Alias", tmdb_id=701)

    db.auto_merge_movie_tmdb_collisions()

    remaining = [movie_id for movie_id in (first["id"], second["id"]) if db.get_movie(movie_id)]
    assert len(remaining) == 1
