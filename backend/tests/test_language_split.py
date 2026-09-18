"""Retroactive movie/series language split: catalog entries auto-merged
together (by a shared tmdb_id) before the auto-merge language gate existed
can still carry sources spanning more than one non-overlapping language
under one row. movie_language_split_dry_run_report/apply_movie_language_
split (and the series equivalents) find and undo exactly that.
"""

import vod_db


def _movie_source(provider_id, movie_id, stream_id, language):
    conn = vod_db._connect()
    conn.execute(
        "INSERT INTO movie_sources (movie_id, provider_id, provider_stream_id, container_extension, raw_name, language, added_at, last_seen_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (movie_id, provider_id, stream_id, "mp4", stream_id, language, vod_db._now(), vod_db._now()),
    )
    conn.commit()
    conn.close()


def test_movie_dry_run_report_finds_language_conflict(db):
    provider_id = db.upsert_provider("prov1", "http://example.com", "user", "pass")
    conn = db._connect()
    cur = conn.execute("INSERT INTO movies (name, year, tmdb_id, created_at) VALUES (?,?,?,?)",
                        ("Predator", 1987, "106", db._now()))
    movie_id = cur.lastrowid
    conn.commit()
    conn.close()
    _movie_source(provider_id, movie_id, "en-1", "EN")
    _movie_source(provider_id, movie_id, "de-1", "DE")

    report = db.movie_language_split_dry_run_report()

    assert report["count"] == 1
    entry = report["sample"][0]
    assert entry["movie_id"] == movie_id
    assert entry["name"] == "Predator"
    langs = sorted(g["languages"][0] for g in entry["groups"])
    assert langs == ["DE", "EN"]


def test_movie_dry_run_report_ignores_single_language_movie(db):
    provider_id = db.upsert_provider("prov1", "http://example.com", "user", "pass")
    conn = db._connect()
    cur = conn.execute("INSERT INTO movies (name, year, created_at) VALUES (?,?,?)", ("Solo Movie", 2020, db._now()))
    movie_id = cur.lastrowid
    conn.commit()
    conn.close()
    _movie_source(provider_id, movie_id, "en-1", "EN")
    _movie_source(provider_id, movie_id, "en-2", "EN")

    assert db.movie_language_split_dry_run_report()["count"] == 0


def test_apply_movie_language_split_creates_new_row_and_reassigns_sources(db):
    provider_id = db.upsert_provider("prov1", "http://example.com", "user", "pass")
    conn = db._connect()
    cur = conn.execute(
        "INSERT INTO movies (name, year, tmdb_id, poster_url, created_at) VALUES (?,?,?,?,?)",
        ("Predator", 1987, "106", "http://poster", db._now()),
    )
    movie_id = cur.lastrowid
    category_id = conn.execute(
        "INSERT INTO categories (name, content_type, created_at) VALUES (?,?,?)", ("4K Action", "movie", db._now())
    ).lastrowid
    conn.commit()
    conn.close()
    db.place_movie_in_category(movie_id, category_id)
    _movie_source(provider_id, movie_id, "en-1", "EN")
    _movie_source(provider_id, movie_id, "en-2", "EN")
    _movie_source(provider_id, movie_id, "de-1", "DE")

    result = db.apply_movie_language_split()

    assert result == {"movies_split": 1, "rows_created": 1}

    conn = db._connect()
    # Largest group (2 EN sources) stays on the original row.
    remaining = conn.execute("SELECT COUNT(*) c FROM movie_sources WHERE movie_id=?", (movie_id,)).fetchone()["c"]
    assert remaining == 2

    new_row = conn.execute(
        "SELECT m.id, m.name, m.tmdb_id, m.poster_url FROM movies m "
        "JOIN movie_sources ms ON ms.movie_id = m.id WHERE ms.provider_stream_id='de-1'"
    ).fetchone()
    assert new_row["id"] != movie_id
    assert new_row["name"] == "Predator"
    assert new_row["tmdb_id"] == "106"
    assert new_row["poster_url"] == "http://poster"

    new_placement = conn.execute(
        "SELECT 1 FROM movie_category_placements WHERE movie_id=? AND category_id=?", (new_row["id"], category_id)
    ).fetchone()
    assert new_placement is not None
    conn.close()


def test_apply_movie_language_split_is_idempotent(db):
    provider_id = db.upsert_provider("prov1", "http://example.com", "user", "pass")
    conn = db._connect()
    cur = conn.execute("INSERT INTO movies (name, year, created_at) VALUES (?,?,?)", ("Predator", 1987, db._now()))
    movie_id = cur.lastrowid
    conn.commit()
    conn.close()
    _movie_source(provider_id, movie_id, "en-1", "EN")
    _movie_source(provider_id, movie_id, "de-1", "DE")

    first = db.apply_movie_language_split()
    second = db.apply_movie_language_split()

    assert first == {"movies_split": 1, "rows_created": 1}
    assert second == {"movies_split": 0, "rows_created": 0}


def _series_source(provider_id, series_id, provider_series_id, language):
    conn = vod_db._connect()
    conn.execute(
        "INSERT INTO series_sources (series_id, provider_id, provider_series_id, raw_name, language, added_at, last_seen_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (series_id, provider_id, provider_series_id, provider_series_id, language, vod_db._now(), vod_db._now()),
    )
    conn.commit()
    conn.close()


def _episode(series_id, season, number):
    conn = vod_db._connect()
    cur = conn.execute(
        "INSERT INTO episodes (series_id, season_number, episode_number, name, created_at) VALUES (?,?,?,?,?)",
        (series_id, season, number, f"S{season}E{number}", vod_db._now()),
    )
    episode_id = cur.lastrowid
    conn.commit()
    conn.close()
    return episode_id


def _episode_source(episode_id, provider_id, stream_id):
    conn = vod_db._connect()
    conn.execute(
        "INSERT INTO episode_sources (episode_id, provider_id, provider_stream_id, container_extension, added_at, last_seen_at) "
        "VALUES (?,?,?,?,?,?)",
        (episode_id, provider_id, stream_id, "mp4", vod_db._now(), vod_db._now()),
    )
    conn.commit()
    conn.close()


def test_apply_series_language_split_moves_sources_and_matching_episodes(db):
    en_provider = db.upsert_provider("en-prov", "http://example.com/en", "user", "pass")
    de_provider = db.upsert_provider("de-prov", "http://example.com/de", "user", "pass")
    conn = db._connect()
    cur = conn.execute("INSERT INTO series (name, year, tmdb_id, created_at) VALUES (?,?,?,?)",
                        ("Dark", 2017, "77177", db._now()))
    series_id = cur.lastrowid
    conn.commit()
    conn.close()
    _series_source(en_provider, series_id, "en-series-1", "EN")
    _series_source(de_provider, series_id, "de-series-1", "DE")

    ep1 = _episode(series_id, 1, 1)
    _episode_source(ep1, en_provider, "en-ep1")
    _episode_source(ep1, de_provider, "de-ep1")
    ep2 = _episode(series_id, 1, 2)
    _episode_source(ep2, de_provider, "de-ep2")  # DE-only episode

    result = db.apply_series_language_split()

    assert result == {"series_split": 1, "rows_created": 1}

    conn = db._connect()
    # Original series kept the EN series_source and ep1's EN episode_source,
    # plus ep1 survives with just that source (mixed episode: DE source moved).
    remaining_series_sources = conn.execute(
        "SELECT provider_id FROM series_sources WHERE series_id=?", (series_id,)
    ).fetchall()
    assert [r["provider_id"] for r in remaining_series_sources] == [en_provider]

    ep1_row = conn.execute("SELECT id FROM episodes WHERE id=?", (ep1,)).fetchone()
    assert ep1_row is not None
    ep1_sources = conn.execute("SELECT provider_id FROM episode_sources WHERE episode_id=?", (ep1,)).fetchall()
    assert [r["provider_id"] for r in ep1_sources] == [en_provider]

    # ep2 was DE-only -- every source moved, so the original ep2 row was purged.
    ep2_row = conn.execute("SELECT id FROM episodes WHERE id=?", (ep2,)).fetchone()
    assert ep2_row is None

    new_series = conn.execute(
        "SELECT s.id, s.name, s.tmdb_id FROM series s JOIN series_sources ss ON ss.series_id=s.id WHERE ss.provider_id=?",
        (de_provider,),
    ).fetchone()
    assert new_series["id"] != series_id
    assert new_series["name"] == "Dark"
    assert new_series["tmdb_id"] == "77177"

    new_episodes = conn.execute(
        "SELECT season_number, episode_number FROM episodes WHERE series_id=? ORDER BY episode_number", (new_series["id"],)
    ).fetchall()
    # Both S1E1 (DE source moved off the mixed episode) and S1E2 (DE-only,
    # whole episode moved) now exist under the new DE series.
    assert [(r["season_number"], r["episode_number"]) for r in new_episodes] == [(1, 1), (1, 2)]
    conn.close()


def test_series_dry_run_report_ignores_single_language_series(db):
    provider_id = db.upsert_provider("prov1", "http://example.com", "user", "pass")
    conn = db._connect()
    cur = conn.execute("INSERT INTO series (name, year, created_at) VALUES (?,?,?)", ("Solo Show", 2020, db._now()))
    series_id = cur.lastrowid
    conn.commit()
    conn.close()
    _series_source(provider_id, series_id, "s-1", "EN")

    assert db.series_language_split_dry_run_report()["count"] == 0


def test_apply_series_language_split_is_idempotent(db):
    en_provider = db.upsert_provider("en-prov", "http://example.com/en", "user", "pass")
    de_provider = db.upsert_provider("de-prov", "http://example.com/de", "user", "pass")
    conn = db._connect()
    cur = conn.execute("INSERT INTO series (name, year, created_at) VALUES (?,?,?)", ("Dark", 2017, db._now()))
    series_id = cur.lastrowid
    conn.commit()
    conn.close()
    _series_source(en_provider, series_id, "en-series-1", "EN")
    _series_source(de_provider, series_id, "de-series-1", "DE")

    first = db.apply_series_language_split()
    second = db.apply_series_language_split()

    assert first == {"series_split": 1, "rows_created": 1}
    assert second == {"series_split": 0, "rows_created": 0}
