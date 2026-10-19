"""Separate Movies / TV shows download folders (and the shared-folder default)."""

import asyncio

import pytest
from fastapi import HTTPException

import config
import vod_db
import vod_downloader
import vod_routes

from test_downloads import FILE_BYTES, _movie, _run, server  # noqa: F401


def _settings(**kw):
    base = {"enabled": True, "output_dir": "", "movies_dir": "", "shows_dir": "", "min_free_gb": 0}
    base.update(kw)
    config.save_download_settings(base)


def test_default_layout_uses_movies_and_shows_inside_the_download_folder(db, tmp_path):
    _settings(output_dir=str(tmp_path / "dl"))
    r = vod_downloader.roots_for(config.get_download_settings())
    assert r["movie"] == tmp_path / "dl" / "Movies" and r["episode"] == tmp_path / "dl" / "Shows"


def test_separate_folders_replace_the_subfolders(db, tmp_path):
    _settings(output_dir=str(tmp_path / "dl"), movies_dir=str(tmp_path / "films"), shows_dir=str(tmp_path / "tv"))
    r = vod_downloader.roots_for(config.get_download_settings())
    assert r["movie"] == tmp_path / "films" and r["episode"] == tmp_path / "tv"


def test_one_separate_folder_leaves_the_other_kind_in_the_download_folder(db, tmp_path):
    _settings(output_dir=str(tmp_path / "dl"), movies_dir=str(tmp_path / "films"))
    r = vod_downloader.roots_for(config.get_download_settings())
    assert r["movie"] == tmp_path / "films" and r["episode"] == tmp_path / "dl" / "Shows"


def test_the_shared_folder_is_optional_when_both_are_set(db, tmp_path):
    assert vod_downloader.validate_settings({"movies_dir": str(tmp_path / "a"), "shows_dir": str(tmp_path / "b")}) is None
    assert "Choose a download folder" in vod_downloader.validate_settings({"movies_dir": str(tmp_path / "a")})
    assert "Choose a download folder" in vod_downloader.validate_settings({})


def test_folder_validation_messages(db, tmp_path):
    ok = str(tmp_path / "x")
    assert "absolute" in vod_downloader.validate_settings({"output_dir": ok, "movies_dir": "relative/films"})
    assert "different folders" in vod_downloader.validate_settings({"movies_dir": ok, "shows_dir": ok})
    config.save_strm_export_settings({"output_dir": str(tmp_path / "strm")})
    assert ".strm export" in vod_downloader.validate_settings({"output_dir": ok, "shows_dir": str(tmp_path / "strm")})


def test_movie_and_episode_land_in_their_own_folders(db, server, tmp_path):
    films, tv = tmp_path / "films", tmp_path / "tv"
    _settings(movies_dir=str(films), shows_dir=str(tv))
    mid, pid = _movie(db, server)
    sid = db.upsert_series("Great Show", 2015)
    eid = db.add_episode(sid, 1, 2, "Two")
    db.add_episode_source(eid, pid, "55", "mp4")
    vod_downloader.enqueue(movie_ids=[mid], episode_ids=[eid])
    assert _run(1)["status"] == "done" and _run(2)["status"] == "done"
    assert (films / "Some Movie (2001) [tmdbid-777]" / "Some Movie (2001).mp4").read_bytes() == FILE_BYTES
    assert (tv / "Great Show (2015)" / "Season 01" / "Great Show - S01E02.mp4").read_bytes() == FILE_BYTES
    assert not (films / "Movies").exists() and not (tv / "Shows").exists()
    # the save-to-device link finds each file under its own root
    assert vod_downloader.file_for_job(1).parent.parent == films.resolve()
    assert vod_downloader.file_for_job(2).name == "Great Show - S01E02.mp4"


def test_tmdb_id_in_folder_names_is_on_by_default(db):
    assert config.get_download_settings()["tmdb_id_in_folder"] is True


def test_tmdb_id_can_be_left_out_of_folder_names(db, server, tmp_path):
    films, tv = tmp_path / "films", tmp_path / "tv"
    _settings(movies_dir=str(films), shows_dir=str(tv), tmdb_id_in_folder=False)
    mid, pid = _movie(db, server)
    sid = db.upsert_series("Great Show", 2015)
    conn = db._connect()
    conn.execute("UPDATE series SET tmdb_id='888' WHERE id=?", (sid,))
    conn.commit()
    conn.close()
    eid = db.add_episode(sid, 1, 2, "Two")
    db.add_episode_source(eid, pid, "55", "mp4")
    vod_downloader.enqueue(movie_ids=[mid], episode_ids=[eid])
    assert _run(1)["status"] == "done" and _run(2)["status"] == "done"
    assert (films / "Some Movie (2001)" / "Some Movie (2001).mp4").read_bytes() == FILE_BYTES
    assert (tv / "Great Show (2015)" / "Season 01" / "Great Show - S01E02.mp4").read_bytes() == FILE_BYTES
    assert vod_downloader.file_for_job(1).parent.name == "Some Movie (2001)"


def test_save_route_keeps_the_tmdb_id_toggle(db, tmp_path):
    body = vod_routes.DownloadSettingsRequest(movies_dir=str(tmp_path / "a"), shows_dir=str(tmp_path / "b"), tmdb_id_in_folder=False)
    asyncio.run(vod_routes.save_download_settings(body))
    assert config.get_download_settings()["tmdb_id_in_folder"] is False


def test_status_reports_free_space_for_each_root(db, tmp_path):
    _settings(movies_dir=str(tmp_path / "films"), shows_dir=str(tmp_path / "tv"))
    free = vod_downloader.get_status()["free"]
    assert free["movies_path"].endswith("films") and free["shows_path"].endswith("tv")
    assert free["movies"] and free["shows"]


def test_save_route_checks_both_folders_are_writable(db, tmp_path):
    films = tmp_path / "films"
    blocker = tmp_path / "blocked"
    blocker.write_text("a file, not a folder")
    body = vod_routes.DownloadSettingsRequest(enabled=True, movies_dir=str(films), shows_dir=str(blocker / "tv"))
    with pytest.raises(HTTPException) as e:
        asyncio.run(vod_routes.save_download_settings(body))
    assert e.value.status_code == 400 and "TV shows folder" in e.value.detail
    ok = vod_routes.DownloadSettingsRequest(enabled=True, movies_dir=str(films), shows_dir=str(tmp_path / "tv"))
    out = asyncio.run(vod_routes.save_download_settings(ok))
    assert out["problem"] is None and films.is_dir() and (tmp_path / "tv").is_dir()
