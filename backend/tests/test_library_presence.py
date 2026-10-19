"""In Library badge: media-server connection settings, the scanner (faked Plex /
Jellyfin / Emby responses), library_presence replace semantics, matching, and the
local-only lookups used by cards, episode lists and the queue dialog."""

import asyncio
import logging
import threading
import time

import pytest
from fastapi import HTTPException

import config
import emby_vod_client
import library_presence
import plex_client
import vod_db
import vod_routes


# ── helpers ──────────────────────────────────────────────────────────────────

def _set(table, row_id, **cols):
    conn = vod_db._connect()
    conn.execute(f"UPDATE {table} SET " + ", ".join(f"{c}=?" for c in cols) + " WHERE id=?", (*cols.values(), row_id))
    conn.commit()
    conn.close()


def _movie(name="Heat", year=1995, tmdb=None, imdb=None):
    mid = vod_db.upsert_movie(name, year)
    _set("movies", mid, tmdb_id=tmdb, imdb_id=imdb)
    return mid


def _series(name="The Office", year=2005, tmdb=None, imdb=None, episodes=((1, 1), (1, 2))):
    sid = vod_db.upsert_series(name, year)
    _set("series", sid, tmdb_id=tmdb, imdb_id=imdb)
    eps = {(s, e): vod_db.add_episode(sid, s, e, f"e{s}{e}") for s, e in episodes}
    return sid, eps


def _plex_server(**kw):
    return {"type": "plex", "name": "Plex", "url": "http://plex:32400", "api_key": "PLEXTOKEN",
            "libraries": [{"key": "1", "title": "Movies", "kind": "movie"},
                          {"key": "2", "title": "TV Shows", "kind": "show"}], **kw}


def _save(servers, scan_hours=6):
    config.save_library_presence_settings({"servers": servers, "scan_hours": scan_hours})
    return config.get_library_presence_settings()


def _row(server="s1", kind="movie", tmdb=None, imdb=None, tvdb=None, title="Heat", year=1995,
         season=None, episode=None, library="Movies"):
    return {"kind": kind, "tmdb_id": tmdb, "imdb_id": imdb, "tvdb_id": tvdb, "title": title, "year": year,
            "season": season, "episode": episode, "library": library}


# ── settings storage ─────────────────────────────────────────────────────────

def test_settings_roundtrip_hides_key_and_keeps_it_when_blank(db):
    s = _save([_plex_server()])
    [srv] = s["servers"]
    assert srv["id"] and srv["type"] == "plex" and srv["url"] == "http://plex:32400" and srv["has_key"] is True
    assert "PLEXTOKEN" not in str(s) and "api_key" not in srv
    assert [lib["title"] for lib in srv["libraries"]] == ["Movies", "TV Shows"]
    assert s["scan_hours"] == 6
    assert config.get_library_server_key(srv["id"]) == "PLEXTOKEN"
    # same id + blank key keeps the saved one; trailing slash trimmed
    _save([{**_plex_server(api_key="", url="http://plex:32400/"), "id": srv["id"]}])
    assert config.get_library_server_key(srv["id"]) == "PLEXTOKEN"
    assert config.get_library_presence_settings()["servers"][0]["url"] == "http://plex:32400"
    # '-' clears it
    _save([{**_plex_server(api_key="-"), "id": srv["id"]}])
    assert config.get_library_server_key(srv["id"]) is None
    assert config.get_library_presence_settings()["servers"][0]["has_key"] is False


def test_multiple_servers_and_bad_type_rejected(db):
    s = _save([_plex_server(), {"type": "jellyfin", "name": "Jelly", "url": "http://jf:8096", "api_key": "JK",
                                "libraries": []}])
    assert [x["type"] for x in s["servers"]] == ["plex", "jellyfin"]
    assert len({x["id"] for x in s["servers"]}) == 2
    with pytest.raises(ValueError):
        _save([{"type": "kodi", "url": "http://x", "api_key": "k"}])


def test_scan_hours_default_and_minimum(db):
    assert config.get_library_presence_settings() == {"servers": [], "scan_hours": 6}
    assert _save([], scan_hours=0)["scan_hours"] == 1


def test_removing_a_server_drops_its_rows(db):
    s = _save([_plex_server()])
    sid = s["servers"][0]["id"]
    vod_db.replace_library_presence(sid, "Plex", [_row(tmdb="949")])
    asyncio.run(vod_routes.save_library_presence_settings(vod_routes.LibraryPresenceSettingsRequest(servers=[], scan_hours=6)))
    assert vod_db.list_library_presence() == []


def _plex_provider(name="PLEX", url="http://plex:32400/", token="PTOKEN", ptype="plex"):
    return vod_db.upsert_provider(name, url, "", token, provider_type=ptype)


def _linked(pid, libraries=None):
    return {"source": "provider", "provider_id": pid,
            "libraries": libraries if libraries is not None else [{"key": "1", "title": "Movies", "kind": "movie"}]}


def test_provider_linked_server_stores_only_a_reference(db):
    pid = _plex_provider()
    _save([_linked(pid)])
    raw = config._read_raw()["library_presence"]["servers"]
    assert raw == [{"id": f"provider:{pid}", "source": "provider", "provider_id": pid,
                    "libraries": [{"key": "1", "title": "Movies", "kind": "movie"}]}]
    [srv] = library_presence.configured_servers()
    assert (srv["id"], srv["source"], srv["type"], srv["name"], srv["url"], srv["has_key"]) == (
        f"provider:{pid}", "provider", "plex", "PLEX", "http://plex:32400", True)
    assert "PTOKEN" not in str(asyncio.run(vod_routes.get_library_presence_settings_route()))


def test_provider_linked_server_reads_token_from_provider_at_scan(db, monkeypatch):
    pid = _plex_provider()
    _save([_linked(pid)])
    assert _plex_provider(url="http://newplex:32400", token="NEWTOKEN") == pid      # edited on the Providers page
    seen = []

    async def fake_fetch(server, key):
        seen.append((server["type"], server["url"], key))
        return [_row(tmdb="949")]

    monkeypatch.setattr(library_presence, "fetch_server", fake_fetch)
    out = asyncio.run(library_presence.scan_all())
    assert seen == [("plex", "http://newplex:32400", "NEWTOKEN")]
    assert out["servers"][0]["ok"] and {r["server"] for r in vod_db.list_library_presence()} == {f"provider:{pid}"}


def test_test_route_with_provider_id_uses_provider_values(db, monkeypatch):
    pid = _plex_provider()
    seen = []

    async def fake_test(server_type, url, key):
        seen.append((server_type, url, key))
        return {"version": "1", "libraries": []}

    monkeypatch.setattr(library_presence, "test_server", fake_test)
    out = asyncio.run(vod_routes.test_library_server(vod_routes.LibraryServerTestRequest(provider_id=pid)))
    assert out["ok"] and seen == [("plex", "http://plex:32400", "PTOKEN")]
    with pytest.raises(HTTPException) as exc:
        asyncio.run(vod_routes.test_library_server(vod_routes.LibraryServerTestRequest(provider_id=9999)))
    assert exc.value.status_code == 404


def test_deleted_provider_drops_the_server_and_its_rows(db, monkeypatch):
    pid = _plex_provider()
    _save([_linked(pid)])
    mid = _movie(tmdb="949")
    vod_db.replace_library_presence(f"provider:{pid}", "PLEX", [_row(tmdb="949")])
    library_presence.rebuild_matches()
    assert vod_db.presence_labels("movie", [mid]) == {mid: "PLEX: Movies"}
    monkeypatch.setattr(vod_routes.vod_importer, "evict_provider_client", _async_noop)
    monkeypatch.setattr(vod_routes.rclone_client, "stop_daemon", _async_noop)
    asyncio.run(vod_routes.delete_provider(pid))
    assert library_presence.configured_servers() == []
    assert asyncio.run(vod_routes.get_library_presence_settings_route())["servers"] == []
    assert vod_db.list_library_presence() == []
    assert vod_db.presence_labels("movie", [mid]) == {}


async def _async_noop(*a, **kw):
    return None


def test_non_media_or_missing_provider_rejected_on_save(db):
    xc = vod_db.upsert_provider("xc", "http://xc", "u", "pw")
    for pid in (xc, 9999):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(vod_routes.save_library_presence_settings(vod_routes.LibraryPresenceSettingsRequest(
                servers=[vod_routes.LibraryServerModel(**_linked(pid))])))
        assert exc.value.status_code == 400


def test_manual_and_provider_servers_side_by_side_and_legacy_entries(db):
    pid = _plex_provider()
    s = _save([_linked(pid), {**_plex_server(name="Friend"), "source": "manual"}])
    assert [(x["source"], x["name"]) for x in library_presence.configured_servers()] == [
        ("provider", "PLEX"), ("manual", "Friend")]
    manual = s["servers"][1]
    assert config.get_library_server_key(manual["id"]) == "PLEXTOKEN"
    # an entry saved before servers had a source counts as manual
    config._update_raw(lambda d: d["library_presence"]["servers"].append(
        {"id": "old1", "type": "emby", "name": "Old", "url": "http://e", "libraries": []}))
    assert [x["source"] for x in config.get_library_presence_settings()["servers"]] == ["provider", "manual", "manual"]


def test_copy_route_is_gone():
    paths = {getattr(r, "path", "") for r in vod_routes.router.routes}
    assert not any("copy-from-provider" in p for p in paths)
    assert not hasattr(vod_routes, "copy_library_server_from_provider")


def test_test_route_uses_saved_key_and_reports_errors(db, monkeypatch):
    sid = _save([_plex_server()])["servers"][0]["id"]
    seen = []

    async def fake_test(server_type, url, key):
        seen.append((server_type, url, key))
        if url == "http://bad":
            raise RuntimeError("401 Unauthorized")
        return {"name": "m1", "libraries": [{"key": "1", "title": "Movies", "kind": "movie"}]}

    monkeypatch.setattr(library_presence, "test_server", fake_test)
    req = vod_routes.LibraryServerTestRequest
    out = asyncio.run(vod_routes.test_library_server(req(id=sid, type="plex", url="http://plex:32400")))
    assert out["ok"] and seen[-1] == ("plex", "http://plex:32400", "PLEXTOKEN")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(vod_routes.test_library_server(req(type="plex", url="http://bad", api_key="x")))
    assert exc.value.status_code == 400 and "401" in exc.value.detail


# ── Plex parsing ─────────────────────────────────────────────────────────────

def _fake_plex(monkeypatch, calls):
    data = {
        ("/library/sections/1/all", 1): [
            {"title": "Heat", "year": 1995, "Guid": [{"id": "imdb://tt0113277"}, {"id": "tmdb://949"}]},
            {"title": "No Ids", "year": 2001},
        ],
        ("/library/sections/2/all", 2): [
            {"ratingKey": "50", "title": "The Office (US)", "year": 2005,
             "Guid": [{"id": "tmdb://2316"}, {"id": "tvdb://73244"}, {"id": "imdb://tt0386676"}]},
        ],
        ("/library/sections/2/all", 4): [
            {"grandparentRatingKey": "50", "grandparentTitle": "The Office (US)", "parentIndex": 1, "index": 2,
             "title": "Diversity Day", "Guid": [{"id": "tvdb://1"}]},
        ],
    }

    async def fake_get(self, path, params=None, timeout=None):
        calls.append((path, dict(params or {}), self.token))
        if path == "/identity":
            return {"MediaContainer": {"machineIdentifier": "m1", "version": "1.40"}}
        if path == "/library/sections":
            return {"MediaContainer": {"Directory": [
                {"key": "1", "title": "Movies", "type": "movie"}, {"key": "2", "title": "TV Shows", "type": "show"},
                {"key": "7", "title": "Music", "type": "artist"}]}}
        items = data[(path, params["type"])]
        return {"MediaContainer": {"Metadata": items, "totalSize": len(items)}}

    monkeypatch.setattr(plex_client.PlexClient, "_get", fake_get)


def test_plex_scan_rows(db, monkeypatch):
    calls = []
    _fake_plex(monkeypatch, calls)
    server = {**_plex_server(), "id": "s1"}
    rows = asyncio.run(library_presence.fetch_server(server, "PLEXTOKEN"))
    by_kind = {}
    for r in rows:
        by_kind.setdefault(r["kind"], []).append(r)
    heat = by_kind["movie"][0]
    assert (heat["tmdb_id"], heat["imdb_id"], heat["title"], heat["year"], heat["library"]) == \
        ("949", "tt0113277", "Heat", 1995, "Movies")
    assert by_kind["movie"][1]["tmdb_id"] is None
    [show] = by_kind["show"]
    assert (show["tmdb_id"], show["tvdb_id"], show["imdb_id"]) == ("2316", "73244", "tt0386676")
    [ep] = by_kind["episode"]
    # an episode carries its show's ids and title, plus season/episode
    assert (ep["tmdb_id"], ep["title"], ep["year"], ep["season"], ep["episode"], ep["library"]) == \
        ("2316", "The Office (US)", 2005, 1, 2, "TV Shows")
    listing = [(p, c.get("type"), c.get("includeGuids")) for p, c, _ in calls]
    assert listing == [("/library/sections/1/all", 1, 1), ("/library/sections/2/all", 2, 1),
                       ("/library/sections/2/all", 4, 1)]
    assert all(tok == "PLEXTOKEN" for *_, tok in calls)


def test_plex_test_lists_movie_and_show_libraries(db, monkeypatch):
    _fake_plex(monkeypatch, [])
    out = asyncio.run(library_presence.test_server("plex", "http://plex:32400", "T"))
    assert out["libraries"] == [{"key": "1", "title": "Movies", "kind": "movie"},
                                {"key": "2", "title": "TV Shows", "kind": "show"}]


# ── Jellyfin / Emby parsing ──────────────────────────────────────────────────

def _fake_emby(monkeypatch, calls):
    items = [
        {"Type": "Movie", "Name": "Heat", "ProductionYear": 1995, "ProviderIds": {"Tmdb": "949", "Imdb": "tt0113277"}},
        {"Type": "Series", "Id": "s9", "Name": "The Office", "ProductionYear": 2005,
         "ProviderIds": {"Tmdb": "2316", "Tvdb": "73244"}},
        {"Type": "Episode", "SeriesId": "s9", "SeriesName": "The Office", "ParentIndexNumber": 2,
         "IndexNumber": 3, "Name": "x", "ProviderIds": {"Tvdb": "1"}},
    ]

    async def fake_get(self, path, params=None, timeout=None):
        calls.append((path, dict(params or {}), self.api_key, self.provider.get("provider_type")))
        if path.endswith("/System/Info"):
            return {"ServerName": "jelly", "Version": "10.9"}
        if path.endswith("/Library/VirtualFolders"):
            return [{"ItemId": "10", "Name": "Movies", "CollectionType": "movies"},
                    {"ItemId": "11", "Name": "Shows", "CollectionType": "tvshows"},
                    {"ItemId": "12", "Name": "Music", "CollectionType": "music"}]
        return {"Items": items, "TotalRecordCount": len(items)}

    monkeypatch.setattr(emby_vod_client.EmbyVodClient, "_get", fake_get)


@pytest.mark.parametrize("server_type", ["jellyfin", "emby"])
def test_jellyfin_emby_scan_rows(db, monkeypatch, server_type):
    calls = []
    _fake_emby(monkeypatch, calls)
    server = {"id": "s2", "type": server_type, "name": "J", "url": "http://jf:8096",
              "libraries": [{"key": "10", "title": "Everything", "kind": "movie"}]}
    rows = asyncio.run(library_presence.fetch_server(server, "JK"))
    kinds = {r["kind"]: r for r in rows}
    assert (kinds["movie"]["tmdb_id"], kinds["movie"]["imdb_id"], kinds["movie"]["year"]) == ("949", "tt0113277", 1995)
    assert (kinds["show"]["tmdb_id"], kinds["show"]["tvdb_id"]) == ("2316", "73244")
    ep = kinds["episode"]
    assert (ep["tmdb_id"], ep["title"], ep["season"], ep["episode"]) == ("2316", "The Office", 2, 3)
    [(path, params, key, ptype)] = calls
    assert path == "/emby/Items" and key == "JK" and ptype == server_type
    assert params["ParentId"] == "10" and params["IncludeItemTypes"] == "Movie,Series,Episode"
    assert "ProviderIds" in params["Fields"] and params["Recursive"] == "true"


def test_jellyfin_test_lists_libraries(db, monkeypatch):
    _fake_emby(monkeypatch, [])
    out = asyncio.run(library_presence.test_server("jellyfin", "http://jf:8096", "JK"))
    assert out["libraries"] == [{"key": "10", "title": "Movies", "kind": "movie"},
                                {"key": "11", "title": "Shows", "kind": "show"}]


# ── scan + replace semantics ─────────────────────────────────────────────────

def test_replace_is_per_server(db):
    vod_db.replace_library_presence("s1", "Plex", [_row(tmdb="1"), _row(tmdb="2")])
    vod_db.replace_library_presence("s2", "Jelly", [_row(server="s2", tmdb="3")])
    vod_db.replace_library_presence("s1", "Plex", [_row(tmdb="4")])
    got = sorted((r["server"], r["tmdb_id"]) for r in vod_db.list_library_presence())
    assert got == [("s1", "4"), ("s2", "3")]
    assert all(r["seen_at"] for r in vod_db.list_library_presence())


def test_scan_all_replaces_and_keeps_failed_server_rows(db, monkeypatch):
    s = _save([_plex_server(name="Plex"), {**_plex_server(name="Other"), "url": "http://bad"}])
    good, bad = (x["id"] for x in s["servers"])
    vod_db.replace_library_presence(bad, "Other", [_row(tmdb="77")])
    vod_db.replace_library_presence(good, "Plex", [_row(tmdb="old")])
    mid = _movie(tmdb="949")

    async def fake_fetch(server, key):
        if server["url"] == "http://bad":
            raise RuntimeError("can't connect")
        return [_row(tmdb="949")]

    monkeypatch.setattr(library_presence, "fetch_server", fake_fetch)
    out = asyncio.run(library_presence.scan_all())
    by_id = {x["id"]: x for x in out["servers"]}
    assert by_id[good]["ok"] and by_id[good]["count"] == 1
    assert not by_id[bad]["ok"] and "can't connect" in by_id[bad]["error"]
    got = {(r["server"], r["tmdb_id"]) for r in vod_db.list_library_presence()}
    assert got == {(bad, "77"), (good, "949")}
    assert vod_db.presence_labels("movie", [mid]) == {mid: "Plex: Movies"}


def _import_mark(tmdb="5", title="Imported", year=2020, age_secs=0):
    vod_db.add_library_presence_rows(library_presence.IMPORT_SERVER, "Radarr",
                                     [_row(tmdb=tmdb, title=title, year=year, library="just imported")])
    if age_secs:
        conn = vod_db._connect()
        conn.execute("UPDATE library_presence SET seen_at=? WHERE server='import'", (str(time.time() - age_secs),))
        conn.commit()
        conn.close()


def _scan_returning(monkeypatch, rows, during=None):
    async def fake_fetch(server, key):
        if during:
            during()
        return rows

    monkeypatch.setattr(library_presence, "fetch_server", fake_fetch)
    return asyncio.run(library_presence.scan_all())


def _import_rows():
    return [r for r in vod_db.list_library_presence() if r["server"] == library_presence.IMPORT_SERVER]


def test_import_mark_survives_a_scan_that_lacks_the_title(db, monkeypatch):
    _save([_plex_server()])
    mid = _movie("Imported", 2020, tmdb="5")
    _import_mark(age_secs=60)                    # the server hasn't indexed the file yet
    _scan_returning(monkeypatch, [_row(tmdb="949")])
    assert len(_import_rows()) == 1
    assert vod_db.presence_labels("movie", [mid]) == {mid: "Radarr: just imported"}


def test_import_mark_dropped_once_a_server_row_matches(db, monkeypatch):
    _save([_plex_server()])
    mid = _movie("Imported", 2020, tmdb="5")
    _import_mark(age_secs=60)
    _scan_returning(monkeypatch, [_row(tmdb="5", title="Imported", year=2020)])
    assert _import_rows() == []
    assert vod_db.presence_labels("movie", [mid]) == {mid: "Plex: Movies"}


def test_import_mark_dropped_after_24_hours(db, monkeypatch):
    _save([_plex_server()])
    mid = _movie("Imported", 2020, tmdb="5")
    _import_mark(age_secs=25 * 3600)
    _scan_returning(monkeypatch, [])
    assert _import_rows() == []
    assert vod_db.presence_labels("movie", [mid]) == {}


def test_import_during_a_scan_survives(db, monkeypatch):
    _save([_plex_server()])
    mid = _movie("Imported", 2020, tmdb="5")
    # lands while fetch_server is running; the listing already has it, but it isn't the scan's to clear
    _scan_returning(monkeypatch, [_row(tmdb="5", title="Imported", year=2020)],
                    during=lambda: library_presence.mark_imported({"kind": "movie", "item_id": mid}))
    assert len(_import_rows()) == 1


def test_import_mark_not_lost_between_rebuild_read_and_write(db, monkeypatch):
    mid = _movie("Imported", 2020, tmdb="5")
    real = vod_db.presence_match_inputs
    racers = []

    def racing_inputs():                         # an import lands after rebuild_matches read the rows
        out = real()
        t = threading.Thread(target=library_presence.mark_imported, args=({"kind": "movie", "item_id": mid},))
        racers.append(t)
        t.start()
        time.sleep(0.2)
        return out

    monkeypatch.setattr(vod_db, "presence_match_inputs", racing_inputs)
    library_presence.rebuild_matches()
    monkeypatch.setattr(vod_db, "presence_match_inputs", real)
    racers[0].join(5)
    assert vod_db.presence_labels("movie", [mid]) == {mid: "Radarr: just imported"}


def test_scan_error_token_redacted_in_results_and_logs(db, monkeypatch, caplog):
    _save([_plex_server()])

    async def fake_fetch(server, key):
        raise RuntimeError("401 for url http://plex:32400/library/sections/1/all?X-Plex-Token=SECRETTOKEN")

    monkeypatch.setattr(library_presence, "fetch_server", fake_fetch)
    with caplog.at_level(logging.WARNING):
        out = asyncio.run(library_presence.scan_all())
    assert "SECRETTOKEN" not in out["servers"][0]["error"] and "X-Plex-Token=" in out["servers"][0]["error"]
    assert "SECRETTOKEN" not in str(library_presence.status()["last_results"])
    assert "SECRETTOKEN" not in caplog.text and "scan of" in caplog.text


# ── matching ─────────────────────────────────────────────────────────────────

def _labels(kind, ids):
    library_presence.rebuild_matches()
    return vod_db.presence_labels(kind, ids)


def test_match_by_tmdb_then_imdb(db):
    by_tmdb = _movie("Totally Different Name", 1980, tmdb="949")
    by_imdb = _movie("Also Different", 1980, imdb="tt0113277")
    vod_db.replace_library_presence("s1", "Plex", [_row(tmdb="949", title="Heat"),
                                                   _row(imdb="tt0113277", title="Heat")])
    assert set(_labels("movie", [by_tmdb, by_imdb])) == {by_tmdb, by_imdb}


def test_title_year_match_within_one_year(db):
    exact = _movie("Amélie", 2001)
    off_by_one = _movie("Spider-Man: Homecoming", 2016)
    too_far = _movie("Dune", 1984)
    vod_db.replace_library_presence("s1", "Plex", [
        _row(title="Amelie", year=2001), _row(title="Spider Man Homecoming", year=2017),
        _row(title="Dune", year=2021)])
    assert set(_labels("movie", [exact, off_by_one, too_far])) == {exact, off_by_one}


def test_title_fallback_not_used_when_ids_conflict(db):
    # a remake with the same title + year window but a different TMDB id is a different film
    mid = _movie("Heat", 1995, tmdb="111")
    vod_db.replace_library_presence("s1", "Plex", [_row(tmdb="949", title="Heat", year=1995)])
    assert _labels("movie", [mid]) == {}


def test_title_without_year_only_when_unique(db):
    unique = _movie("Rare Film", None)
    dup = _movie("Common", None)
    vod_db.replace_library_presence("s1", "Plex", [
        _row(title="Rare Film", year=2003), _row(title="Common", year=1990), _row(title="Common", year=2010)])
    assert set(_labels("movie", [unique, dup])) == {unique}


def test_labels_combine_servers(db):
    mid = _movie(tmdb="949")
    vod_db.replace_library_presence("s1", "Plex", [_row(tmdb="949")])
    vod_db.replace_library_presence("s2", "Jelly", [_row(server="s2", tmdb="949", library="Films")])
    assert _labels("movie", [mid]) == {mid: "Plex: Movies, Jelly: Films"}


def test_series_and_episode_matching(db):
    sid, eps = _series(tmdb="2316")
    vod_db.replace_library_presence("s1", "Plex", [
        _row(kind="show", tmdb="2316", title="The Office (US)", library="TV Shows"),
        _row(kind="episode", tmdb="2316", title="The Office (US)", season=1, episode=2, library="TV Shows")])
    assert _labels("series", [sid]) == {sid: "Plex: TV Shows"}
    assert vod_db.presence_labels("episode", list(eps.values())) == {eps[(1, 2)]: "Plex: TV Shows"}


def test_rebuild_drops_stale_matches(db):
    mid = _movie(tmdb="949")
    vod_db.replace_library_presence("s1", "Plex", [_row(tmdb="949")])
    assert _labels("movie", [mid])
    vod_db.replace_library_presence("s1", "Plex", [])
    assert _labels("movie", [mid]) == {}


# ── lookups used by the UI (local table only) ────────────────────────────────

def test_list_routes_carry_flag_and_filter(db, monkeypatch):
    def boom(*a, **kw):
        raise AssertionError("cards must not call the media server")

    monkeypatch.setattr(library_presence, "fetch_server", boom)
    present = _movie("Heat", 1995, tmdb="949")
    absent = _movie("Other", 2000, tmdb="1")
    sid, eps = _series(tmdb="2316")
    vod_db.replace_library_presence("s1", "Plex", [
        _row(tmdb="949"), _row(kind="show", tmdb="2316", title="The Office"),
        _row(kind="episode", tmdb="2316", title="The Office", season=1, episode=1)])
    library_presence.rebuild_matches()

    out = asyncio.run(vod_routes.list_movies())
    flags = {m["id"]: m["in_library"] for m in out["items"]}
    assert flags == {present: "Plex: Movies", absent: None}
    only = asyncio.run(vod_routes.list_movies(in_library="only"))
    assert [m["id"] for m in only["items"]] == [present] and only["total"] == 1
    hide = asyncio.run(vod_routes.list_movies(in_library="hide"))
    assert [m["id"] for m in hide["items"]] == [absent] and hide["total"] == 1

    series = asyncio.run(vod_routes.list_series())["items"][0]
    assert series["in_library"] == "Plex: Movies"
    assert {e["id"]: e["in_library"] for e in series["episodes"]} == {eps[(1, 1)]: True, eps[(1, 2)]: False}
    assert asyncio.run(vod_routes.list_series(in_library="hide"))["total"] == 0
    detail = asyncio.run(vod_routes.get_series(sid))
    assert detail["in_library"] and {e["episode_number"]: e["in_library"] for e in detail["episodes"]} == {1: True, 2: False}


def test_list_and_detail_routes_make_no_media_server_calls(db, monkeypatch):
    def boom(*a, **kw):
        raise AssertionError("cards must not call the media server")

    for mod, name in ((library_presence, "fetch_server"), (library_presence, "test_server"),
                      (library_presence, "_client"), (plex_client, "PlexClient"), (emby_vod_client, "EmbyVodClient")):
        monkeypatch.setattr(mod, name, boom)
    mid = _movie("Heat", 1995, tmdb="949")
    sid, eps = _series(tmdb="2316")
    vod_db.replace_library_presence("s1", "Plex", [
        _row(tmdb="949"), _row(kind="episode", tmdb="2316", title="The Office", season=1, episode=1)])
    library_presence.rebuild_matches()
    asyncio.run(vod_routes.list_movies(in_library="only"))
    asyncio.run(vod_routes.get_movie(mid))
    asyncio.run(vod_routes.list_series(in_library="hide"))
    asyncio.run(vod_routes.list_series_ids(in_library="only"))
    assert asyncio.run(vod_routes.get_series(sid))["episodes"][0]["in_library"] is True
    out = asyncio.run(vod_routes.downloads_library_check(vod_routes.DownloadQueueRequest(
        movie_ids=[mid], series_ids=[sid])))
    assert out["count"] == 2


def test_bad_in_library_filter_rejected(db):
    with pytest.raises(HTTPException) as exc:
        asyncio.run(vod_routes.list_movies(in_library="maybe"))
    assert exc.value.status_code == 400


# ── post-import marking ──────────────────────────────────────────────────────

from test_arr_handoff import T0, _importing, _job, _tick, handoff, watch  # noqa: E402,F401


def test_successful_handoff_import_marks_movie_present(db, watch):
    jid, _ = _importing(db, watch)
    mid = _job(jid)["item_id"]
    assert vod_db.presence_labels("movie", [mid]) == {}
    _tick(T0 + 30)
    assert _job(jid)["import_status"] == "imported"
    assert vod_db.presence_labels("movie", [mid]) == {mid: "Radarr: just imported"}
    # and it survives a later match rebuild
    assert _labels("movie", [mid]) == {mid: "Radarr: just imported"}


def test_mark_imported_episode_marks_episode_and_series(db):
    sid, eps = _series(tmdb="2316")
    library_presence.mark_imported({"kind": "episode", "item_id": eps[(1, 2)]})
    assert vod_db.presence_labels("episode", list(eps.values())) == {eps[(1, 2)]: "Sonarr: just imported"}
    assert vod_db.presence_labels("series", [sid]) == {sid: "Sonarr: just imported"}
    library_presence.rebuild_matches()
    assert vod_db.presence_labels("episode", list(eps.values())) == {eps[(1, 2)]: "Sonarr: just imported"}


# ── the queue dialog's "already present" check ───────────────────────────────

def test_already_present_check(db):
    present = _movie("Heat", 1995, tmdb="949")
    absent = _movie("Other", 2000)
    sid, eps = _series(tmdb="2316", episodes=((1, 1), (1, 2), (2, 1)))
    vod_db.replace_library_presence("s1", "Plex", [
        _row(tmdb="949"), _row(kind="show", tmdb="2316", title="The Office", library="TV Shows"),
        _row(kind="episode", tmdb="2316", title="The Office", season=1, episode=2, library="TV Shows"),
        _row(kind="episode", tmdb="2316", title="The Office", season=2, episode=1, library="TV Shows")])
    library_presence.rebuild_matches()

    def check(**kw):
        return asyncio.run(vod_routes.downloads_library_check(vod_routes.DownloadQueueRequest(**kw)))

    out = check(movie_ids=[present, absent])
    assert out["count"] == 1 and out["present"][0]["item_id"] == present
    assert out["present"][0]["where"] == "Plex: Movies" and out["present"][0]["title"].startswith("Heat")
    whole = check(series_ids=[sid])
    assert sorted(p["title"] for p in whole["present"]) == ["The Office S01E02", "The Office S02E01"]
    season1 = check(series_ids=[sid], season=1)
    assert [p["item_id"] for p in season1["present"]] == [eps[(1, 2)]]
    picked = check(episode_ids=[eps[(1, 1)], eps[(2, 1)]])
    assert [p["item_id"] for p in picked["present"]] == [eps[(2, 1)]]
    assert check(movie_ids=[absent]) == {"present": [], "count": 0}
