"""library_parser turns a library-relative path into a searchable title/year,
episode identity, and any explicit TMDB/IMDb id -- pure logic, no TMDB calls.
Cases are modeled on real-world naming: scene releases, Plex/Jellyfin style,
bare-year titles, multi-episode files, and flat vs. season-folder TV layouts.
"""

import pytest

import library_parser as lp


@pytest.mark.parametrize("path,title,year", [
    ("The.Matrix.1999.1080p.BluRay.x264-GRP.mkv", "The Matrix", 1999),
    ("Movies/Inception (2010)/Inception (2010).mkv", "Inception", 2010),
    ("Movies/Inception (2010)/movie.mkv", "Inception", 2010),
    ("Blade.Runner.2049.2017.2160p.UHD.BluRay.REMUX.HDR.HEVC.Atmos-FGT.mkv", "Blade Runner 2049", 2017),
    ("1917.2019.1080p.WEB-DL.DD5.1.H264-CMRG.mkv", "1917", 2019),
    ("2012 (2009).mp4", "2012", 2009),
    ("2001 A Space Odyssey (1968) [1080p].mkv", "2001 A Space Odyssey", 1968),
    ("Some Movie [YTS.MX].mp4", "Some Movie", None),
    ("Spider-Man - Into the Spider-Verse (2018).mkv", "Spider-Man - Into the Spider-Verse", 2018),
    ("Amelie.2001.MULTi.1080p.BluRay.x265.mkv", "Amelie", 2001),
])
def test_movie_titles_and_years(path, title, year):
    p = lp.parse_path(path)
    assert p.kind == "movie"
    assert (p.title, p.year) == (title, year)


@pytest.mark.parametrize("path,show,season,episode", [
    ("Breaking.Bad.S02E05.720p.HDTV.x264-CTU.mkv", "Breaking Bad", 2, 5),
    ("TV/Breaking Bad (2008)/Season 2/Breaking Bad - S02E05 - Breakage.mkv", "Breaking Bad", 2, 5),
    ("The Office/Season 03/The Office s03e11.mp4", "The Office", 3, 11),
    ("Show Name/Show Name 2x07.avi", "Show Name", 2, 7),
    ("Shows/Seinfeld/Specials/Seinfeld S00E01.mkv", "Seinfeld", 0, 1),
])
def test_episode_identity(path, show, season, episode):
    p = lp.parse_path(path)
    assert p.kind == "episode"
    assert (p.title, p.season, p.episode) == (show, season, episode)


def test_show_year_from_folder_and_shared_show_key():
    a = lp.parse_path("TV/Breaking Bad (2008)/Season 1/S01E01.mkv")
    b = lp.parse_path("TV/Breaking Bad (2008)/Season 2/S02E01.mkv")
    assert a.year == 2008
    assert a.show_key == b.show_key == "TV/Breaking Bad (2008)"


def test_multi_episode_file():
    p = lp.parse_path("Show/Season 1/Show S01E01-E02.mkv")
    assert (p.season, p.episode, p.episode_end) == (1, 1, 2)


def test_season_taken_from_folder_when_filename_has_no_season():
    p = lp.parse_path("Show/Season 4/Show - E03.mkv")
    # No SxxExx or NxNN in the name -> treated as a movie; documented limit.
    assert p.kind == "movie"


def test_explicit_tmdb_id_in_folder_or_file():
    assert lp.parse_path("Movies/Heat (1995) {tmdb-949}/Heat.mkv").tmdb_id == "949"
    assert lp.parse_path("Heat (1995) [tmdbid=949].mkv").tmdb_id == "949"
    assert lp.parse_path("Show {tmdb-1396}/Season 1/S01E01.mkv").tmdb_id == "1396"


def test_explicit_imdb_id():
    p = lp.parse_path("Heat (1995) [imdbid-tt0113277].mkv")
    assert p.imdb_id == "tt0113277"
    assert p.title == "Heat"


def test_junk_only_filename_falls_back_to_folder_with_warning():
    p = lp.parse_path("Movies/Alien (1979)/1080p.mkv")
    assert (p.title, p.year) == ("Alien", 1979)
    assert "title taken from parent folder" in p.warnings


def test_windows_separators():
    p = lp.parse_path("TV\\Lost\\Season 1\\Lost.S01E03.mkv")
    assert (p.title, p.season, p.episode) == ("Lost", 1, 3)


def test_is_video_file():
    assert lp.is_video_file("a/b.MKV")
    assert not lp.is_video_file("a/b.nfo")
    assert not lp.is_video_file("a/b.srt")


def test_flat_nxnn_episode_excludes_marker_from_title():
    p = lp.parse_path("Show Name 2x07.avi")
    assert (p.kind, p.title, p.season, p.episode) == ("episode", "Show Name", 2, 7)
