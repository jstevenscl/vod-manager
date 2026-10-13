"""The .strm exporter: layout, URLs, skip rules, mirror-mode safety."""

import pytest

import strm_export


def _library(db):
    """Two movies (one with a TMDB id, one without), one show with two
    episodes, all placed in categories; returns (client, ids)."""
    provider = db.upsert_provider("prov1", "http://example.com", "user", "pass")
    db.bulk_import_movies(provider, [
        {"name": "Heat", "year": 1995, "provider_stream_id": "m1", "container_extension": "mkv", "raw_name": "Heat", "_has_detail": True},
        {"name": "No Match: A Film?", "year": 2001, "provider_stream_id": "m2", "container_extension": "mp4", "raw_name": "No Match", "_has_detail": True},
    ])
    heat = db.get_movie_by_name_year("Heat", 1995)["id"]
    nomatch = db.get_movie_by_name_year("No Match: A Film?", 2001)["id"]
    db.backfill_tmdb_id_if_missing("movie", heat, "949")
    movie_cat = db.upsert_category("Action", "movie")
    other_cat = db.upsert_category("Classics", "movie")
    for movie_id in (heat, nomatch):
        db.place_movie_in_category(movie_id, movie_cat)
    db.place_movie_in_category(heat, other_cat)  # same movie in a 2nd category -> still one file

    series = db.upsert_series("Breaking Bad", 2008)
    db.backfill_tmdb_id_if_missing("series", series, "1396")
    for n, name in ((1, "Pilot"), (2, "Cat's in the Bag")):
        ep = db.add_episode(series, 1, n, name)
        db.add_episode_source(ep, provider, f"e{n}", "mp4", raw_name=name)
    series_cat = db.upsert_category("Drama", "series")
    db.place_series_in_category(series, series_cat)
    client = db.create_xc_client("Emby")
    return client, {"movie_cat": movie_cat, "other_cat": other_cat, "series_cat": series_cat}


def _settings(tmp_path, client, **overrides):
    base = {
        "output_dir": str(tmp_path / "strm"), "base_url": "http://vm.local:8282/", "xc_client_id": client["id"],
        "include_movies": True, "include_series": True, "include_unmatched": False, "mirror": True, "interval_hours": 0,
    }
    base.update(overrides)
    return base


def test_safe_name_strips_illegal_characters_and_trailing_dots():
    assert strm_export.safe_name('Mission: Impossible?') == "Mission Impossible"
    assert strm_export.safe_name("What...  ") == "What"
    assert strm_export.safe_name("///") == "Untitled"
    assert len(strm_export.safe_name("x" * 500)) <= 120


def test_export_writes_emby_layout_and_urls(db, tmp_path):
    client, _ = _library(db)

    result = strm_export.run_export(_settings(tmp_path, client))

    root = tmp_path / "strm"
    movie = root / "Movies" / "Heat (1995) [tmdbid-949]" / "Heat (1995).strm"
    assert movie.read_text().strip().startswith(f"http://vm.local:8282/movie/{client['username']}/")
    assert movie.read_text().strip().endswith(".mkv")
    ep = root / "Shows" / "Breaking Bad (2008) [tmdbid-1396]" / "Season 01" / "Breaking Bad - S01E02.strm"
    assert "/series/" in ep.read_text() and ep.read_text().strip().endswith(".mp4")
    assert (root / strm_export.MARKER_FILE).exists()
    assert result["movies"] == 1 and result["episodes"] == 2 and result["series"] == 1
    assert result["skipped_no_tmdb"] == 1  # the movie without a TMDB id


def test_unmatched_items_go_to_an_unmatched_folder_when_enabled(db, tmp_path):
    client, _ = _library(db)

    strm_export.run_export(_settings(tmp_path, client, include_unmatched=True))

    names = [p.name for p in (tmp_path / "strm" / "Movies" / "Unmatched").iterdir()]
    assert len(names) == 1 and names[0].startswith("No Match A Film (2001)")


def test_dry_run_touches_nothing(db, tmp_path):
    client, _ = _library(db)

    result = strm_export.run_export(_settings(tmp_path, client), dry_run=True)

    assert result["total"] == 3 and result["written"] == 3 and result["dry_run"] is True
    assert not (tmp_path / "strm").exists()


def test_rerun_leaves_unchanged_files_alone(db, tmp_path):
    client, _ = _library(db)
    settings = _settings(tmp_path, client)
    strm_export.run_export(settings)

    again = strm_export.run_export(settings)

    assert again["written"] == 0 and again["updated"] == 0 and again["unchanged"] == 3


def test_category_allowlist_limits_what_is_exported(db, tmp_path):
    client, ids = _library(db)
    db.update_xc_client(client["id"], category_allowlist=str(ids["series_cat"]))

    result = strm_export.run_export(_settings(tmp_path, client))

    assert result["movies"] == 0 and result["episodes"] == 2


def test_mirror_removes_stale_strm_and_empty_folders_but_nothing_else(db, tmp_path):
    client, ids = _library(db)
    settings = _settings(tmp_path, client)
    strm_export.run_export(settings)
    root = tmp_path / "strm"
    (root / "Movies" / "notes.txt").write_text("keep me")
    stale = root / "Movies" / "Gone (1999) [tmdbid-1]"
    stale.mkdir()
    (stale / "Gone (1999).strm").write_text("http://x/movie/a/b/1.mp4")

    result = strm_export.run_export(settings)

    assert result["removed"] == 1
    assert not stale.exists()
    assert (root / "Movies" / "notes.txt").read_text() == "keep me"

    kept = strm_export.run_export(_settings(tmp_path, client, mirror=False))
    assert kept["removed"] == 0


def test_refuses_a_non_empty_folder_it_did_not_create(db, tmp_path):
    client, _ = _library(db)
    foreign = tmp_path / "strm"
    foreign.mkdir()
    (foreign / "family-photos.jpg").write_text("x")

    with pytest.raises(ValueError, match="not empty"):
        strm_export.run_export(_settings(tmp_path, client))


@pytest.mark.parametrize("overrides,fragment", [
    ({"output_dir": ""}, "output folder"),
    ({"output_dir": "relative/path"}, "absolute"),
    ({"base_url": "emby.local"}, "base URL"),
    ({"xc_client_id": None}, "XC user"),
    ({"include_movies": False, "include_series": False}, "Movies, TV Shows or both"),
])
def test_settings_validation(db, tmp_path, overrides, fragment):
    client, _ = _library(db)
    with pytest.raises(ValueError, match=fragment):
        strm_export.run_export(_settings(tmp_path, client, **overrides))


def test_only_one_export_at_a_time(db, tmp_path):
    client, _ = _library(db)
    assert strm_export._run_lock.acquire(blocking=False)
    try:
        with pytest.raises(RuntimeError, match="already running"):
            strm_export.run_tracked_export(_settings(tmp_path, client))
    finally:
        strm_export._run_lock.release()


def test_settings_route_persists_and_run_route_uses_them(db, tmp_path, monkeypatch):
    import asyncio
    import config
    import vod_routes

    client, _ = _library(db)
    body = vod_routes.StrmExportSettingsRequest(
        output_dir=str(tmp_path / "strm"), base_url="http://vm.local:8282/", xc_client_id=client["id"],
    )

    saved = asyncio.run(vod_routes.save_strm_export_settings(body))

    assert saved["settings"]["output_dir"] == str(tmp_path / "strm")
    assert saved["settings"]["base_url"] == "http://vm.local:8282"
    assert config.get_strm_export_settings()["xc_client_id"] == client["id"]
    result = asyncio.run(vod_routes.run_strm_export(dry_run=True))
    assert result["total"] == 3
