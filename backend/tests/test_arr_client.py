"""Radarr/Sonarr v3 API client used by the library handoff."""

import json

import httpx
import pytest

from arr_client import ArrClient, ArrError


def _client(kind, handler):
    return ArrClient(kind, "http://arr:7878/", "KEY", transport=httpx.MockTransport(handler))


def _record(responses):
    """Handler that records requests and answers by (method, path)."""
    seen = []

    def handler(request):
        seen.append(request)
        body = responses.get((request.method, request.url.path))
        if callable(body):
            body = body(request)
        return httpx.Response(200, json=body if body is not None else {})
    return handler, seen


def test_lookup_by_tmdb_uses_tmdb_term():
    handler, seen = _record({("GET", "/api/v3/movie/lookup"): [{"title": "The Matrix", "year": 1999, "tmdbId": 603}]})
    item = _client("radarr", handler).lookup(603, "The Matrix", 1999)
    assert seen[0].url.params["term"] == "tmdb:603"
    assert seen[0].headers["X-Api-Key"] == "KEY"
    assert item["tmdbId"] == 603


def test_lookup_by_title_requires_single_exact_year_match():
    results = [{"title": "Dune", "year": 1984}, {"title": "Dune", "year": 2021}, {"title": "Dune: Part Two", "year": 2024}]
    handler, seen = _record({("GET", "/api/v3/movie/lookup"): results})
    c = _client("radarr", handler)
    assert c.lookup(None, "Dune", 2021)["year"] == 2021
    assert seen[0].url.params["term"] == "Dune"
    assert c.lookup(None, "Dune", 2020) is None


def test_lookup_title_ambiguous_without_year_is_none():
    handler, _ = _record({("GET", "/api/v3/series/lookup"): [{"title": "Dune", "year": 1984}, {"title": "Dune", "year": 2021}]})
    assert _client("sonarr", handler).lookup(None, "Dune", None) is None


def test_get_existing_only_returns_items_in_library():
    handler, _ = _record({("GET", "/api/v3/movie/lookup"): [{"title": "X", "year": 2000, "tmdbId": 1}]})
    assert _client("radarr", handler).get_existing(1, "X", 2000) is None
    handler, _ = _record({("GET", "/api/v3/movie/lookup"): [{"title": "X", "year": 2000, "tmdbId": 1, "id": 42}]})
    assert _client("radarr", handler).get_existing(1, "X", 2000)["id"] == 42


def test_add_movie_never_searches():
    handler, seen = _record({("POST", "/api/v3/movie"): lambda r: {**json.loads(r.content), "id": 7}})
    out = _client("radarr", handler).add({"title": "X", "tmdbId": 1}, "/media/Movies", 6, "unmonitored")
    body = json.loads(seen[0].content)
    assert body["addOptions"]["searchForMovie"] is False
    assert body["monitored"] is False
    assert body["rootFolderPath"] == "/media/Movies" and body["qualityProfileId"] == 6
    assert out["id"] == 7


def test_add_movie_monitored_when_asked():
    handler, seen = _record({("POST", "/api/v3/movie"): {"id": 7}})
    _client("radarr", handler).add({"title": "X", "tmdbId": 1}, "/m", 6, "monitored")
    assert json.loads(seen[0].content)["monitored"] is True


def test_add_series_future_monitor_never_searches():
    handler, seen = _record({("POST", "/api/v3/series"): {"id": 9}})
    _client("sonarr", handler).add({"title": "S", "tvdbId": 5}, "/media/Tv", 6, "future")
    body = json.loads(seen[0].content)
    assert body["addOptions"] == {"monitor": "future", "searchForMissingEpisodes": False,
                                  "searchForCutoffUnmetEpisodes": False}
    assert body["seasonFolder"] is True and body["monitored"] is True


def test_import_path_posts_command():
    handler, seen = _record({("POST", "/api/v3/command"): {"id": 55}})
    assert _client("radarr", handler).import_path("/finished/radarr/X (2000)") == 55
    assert json.loads(seen[0].content) == {"name": "DownloadedMoviesScan", "path": "/finished/radarr/X (2000)",
                                           "importMode": "Move"}
    handler, seen = _record({("POST", "/api/v3/command"): {"id": 56}})
    _client("sonarr", handler).import_path("/finished/tv/S/Season 01/S - S01E02.mkv")
    assert json.loads(seen[0].content)["name"] == "DownloadedEpisodesScan"


def test_command_status():
    handler, _ = _record({("GET", "/api/v3/command/55"): {"id": 55, "status": "completed"}})
    assert _client("radarr", handler).command(55)["status"] == "completed"


def test_has_file_movie():
    handler, _ = _record({("GET", "/api/v3/movie/7"): {"id": 7, "hasFile": True}})
    assert _client("radarr", handler).has_file(7) is True


def test_has_file_episode_matches_season_and_number():
    eps = [{"seasonNumber": 1, "episodeNumber": 1, "hasFile": True},
           {"seasonNumber": 1, "episodeNumber": 2, "hasFile": False},
           {"seasonNumber": 2, "episodeNumber": 1, "hasFile": True}]
    handler, seen = _record({("GET", "/api/v3/episode"): eps})
    c = _client("sonarr", handler)
    assert c.has_file(9, 1, 2) is False
    assert c.has_file(9, 2, 1) is True
    assert seen[0].url.params["seriesId"] == "9"


def test_rejection_for_joins_reasons():
    handler, _ = _record({("GET", "/api/v3/manualimport"): [
        {"rejections": [{"reason": "Not an upgrade"}, {"reason": "Custom Format score 0 below minimum 1"}]}]})
    msg = _client("radarr", handler).rejection_for("/finished/radarr/X")
    assert "Not an upgrade" in msg and "below minimum" in msg


def test_rejection_for_none_when_clean():
    handler, _ = _record({("GET", "/api/v3/manualimport"): [{"rejections": []}]})
    assert _client("radarr", handler).rejection_for("/p") is None


def test_root_folders_and_profiles():
    handler, _ = _record({("GET", "/api/v3/rootfolder"): [{"id": 1, "path": "/media/Movies", "freeSpace": 1}],
                          ("GET", "/api/v3/qualityprofile"): [{"id": 6, "name": "HD", "items": []}]})
    c = _client("radarr", handler)
    assert c.root_folders() == [{"id": 1, "path": "/media/Movies"}]
    assert c.quality_profiles() == [{"id": 6, "name": "HD"}]


def test_http_401_raises_arr_error_with_hint():
    c = _client("radarr", lambda r: httpx.Response(401, json={}))
    with pytest.raises(ArrError) as exc:
        c.system_status()
    assert "API key" in str(exc.value) and "Radarr" in str(exc.value)


def test_connection_error_raises_arr_error():
    def handler(request):
        raise httpx.ConnectError("refused")
    with pytest.raises(ArrError) as exc:
        _client("sonarr", handler).system_status()
    assert "Sonarr" in str(exc.value)
