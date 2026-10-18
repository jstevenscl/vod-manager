"""In Library badge: which titles the user's own media servers (Plex, Jellyfin,
Emby) already hold.

A server is either one of the Plex / Jellyfin / Emby providers, linked by id (its
URL and token are read from the provider each time, so a token changed on the
Providers page applies here too, and deleting the provider drops the server), or
one typed into the In Library card for a server that isn't a provider. Media
servers are called only by the Test button, the library listing and a scan;
cards, episode lists and the queue check read the local tables:

  library_presence        what each server holds, replaced per server on each scan
  library_presence_items  which movies / series / episodes matched, rebuilt locally

Matching: TMDB id, then IMDb id, then normalized title + year (+-1). The title
fallback never matches across conflicting ids (a remake with its own TMDB id is a
different title), and without a year on either side it needs a unique title.
"""

import asyncio
import logging
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone

import config
import emby_vod_client
import plex_client
import strm_export
import vod_db
from library_matcher import normalize_title

logger = logging.getLogger(__name__)

IMPORT_SERVER = "import"                 # rows a Radarr/Sonarr import just added
_IMPORT_LIBRARY = "just imported"
_LOOP_SECS = 600                         # local re-match check; scans follow scan_hours
_scan_lock = asyncio.Lock()
_last_scan: dict = {"at": None, "servers": []}


# ── media-server calls (Test, library listing, scan) ─────────────────────────

def _client(server_type: str, url: str, key: str, native_api_paths: bool = False):
    if server_type == "plex":
        return plex_client.PlexClient({"base_url": url, "password": key})
    return emby_vod_client.EmbyVodClient({"base_url": url, "password": key, "provider_type": server_type,
                                          "native_api_paths": native_api_paths})


def _redact(server_type: str, exc: Exception) -> str:
    return (plex_client._redact(exc) if server_type == "plex" else emby_vod_client._redact(exc)) or type(exc).__name__


def _int(v) -> int | None:
    try:
        return int(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _plex_ids(item: dict) -> dict:
    ids = {"tmdb_id": None, "imdb_id": None, "tvdb_id": None}
    for guid in item.get("Guid") or []:
        scheme, _, value = (guid.get("id") or "").partition("://")
        if value and f"{scheme}_id" in ids:
            ids[f"{scheme}_id"] = value
    return ids


def _emby_ids(item: dict) -> dict:
    p = {k.lower(): v for k, v in (item.get("ProviderIds") or {}).items()}
    return {"tmdb_id": p.get("tmdb") or None, "imdb_id": p.get("imdb") or None, "tvdb_id": p.get("tvdb") or None}


async def test_server(server_type: str, url: str, key: str) -> dict:
    """Checks the connection and lists the movie / show libraries to pick from."""
    async with _client(server_type, url, key) as client:
        info = await client.test_connection()
        libs = await client.list_libraries()
    if server_type == "plex":
        libraries = [{"key": str(d["key"]), "title": d.get("title") or "", "kind": d["type"]}
                     for d in libs if d.get("type") in ("movie", "show")]
        version = info.get("version")
    else:
        kinds = {"movies": "movie", "tvshows": "show", "mixed": "mixed", None: "mixed"}
        libraries = [{"key": str(d["ItemId"]), "title": d.get("Name") or "", "kind": kinds[d.get("CollectionType")]}
                     for d in libs if d.get("CollectionType") in kinds and d.get("ItemId")]
        version = (info or {}).get("Version")
    return {"version": version, "libraries": libraries}


async def fetch_server(server: dict, key: str) -> list[dict]:
    """One listing per chosen library (paged), with external ids. Episode rows carry
    their show's ids, title and year."""
    rows: list[dict] = []
    async with _client(server["type"], server["url"], key, bool(server.get("native_api_paths"))) as client:
        for lib in server.get("libraries") or []:
            if server["type"] == "plex":
                rows.extend(await _plex_library(client, lib))
            else:
                rows.extend(await _emby_library(client, lib))
    return rows


async def _plex_library(client, lib: dict) -> list[dict]:
    name = lib.get("title") or ""
    if lib.get("kind") == "movie":
        return [{"kind": "movie", "library": name, "title": m.get("title"), "year": _int(m.get("year")),
                 **_plex_ids(m)} for m in await client._list_all_paged(lib["key"], 1)]
    shows = {}
    rows = []
    for s in await client._list_all_paged(lib["key"], 2):
        row = {"kind": "show", "library": name, "title": s.get("title"), "year": _int(s.get("year")), **_plex_ids(s)}
        shows[str(s.get("ratingKey"))] = row
        rows.append(row)
    for e in await client._list_all_paged(lib["key"], 4):        # every episode of the library in one listing
        show = shows.get(str(e.get("grandparentRatingKey"))) or {
            "title": e.get("grandparentTitle"), "year": None, "tmdb_id": None, "imdb_id": None, "tvdb_id": None}
        rows.append({**{k: show[k] for k in ("title", "year", "tmdb_id", "imdb_id", "tvdb_id")}, "kind": "episode",
                     "library": name, "season": _int(e.get("parentIndex")), "episode": _int(e.get("index"))})
    return rows


async def _emby_library(client, lib: dict) -> list[dict]:
    name = lib.get("title") or ""
    items = await client._list_items_paged({
        "ParentId": lib["key"], "IncludeItemTypes": "Movie,Series,Episode", "Recursive": "true",
        "Fields": "ProviderIds,ProductionYear",
    })
    rows, shows, episodes = [], {}, []
    for it in items:
        kind = {"Movie": "movie", "Series": "show", "Episode": "episode"}.get(it.get("Type"))
        if kind == "episode":
            episodes.append(it)
        elif kind:
            row = {"kind": kind, "library": name, "title": it.get("Name"), "year": _int(it.get("ProductionYear")),
                   **_emby_ids(it)}
            rows.append(row)
            if kind == "show":
                shows[str(it.get("Id"))] = row
    for e in episodes:
        show = shows.get(str(e.get("SeriesId"))) or {
            "title": e.get("SeriesName"), "year": None, "tmdb_id": None, "imdb_id": None, "tvdb_id": None}
        rows.append({**{k: show[k] for k in ("title", "year", "tmdb_id", "imdb_id", "tvdb_id")}, "kind": "episode",
                     "library": name, "season": _int(e.get("ParentIndexNumber")), "episode": _int(e.get("IndexNumber"))})
    return rows


def configured_servers(with_keys: bool = False) -> list[dict]:
    """The saved servers with each provider link filled in from its provider row
    ({id, source, provider_id, type, name, url, has_key, libraries}, plus "key" when
    with_keys). A link whose provider was deleted, or is no longer Plex / Jellyfin /
    Emby, is left out."""
    out = []
    for srv in config.get_library_presence_settings()["servers"]:
        if srv["source"] == "provider":
            provider = vod_db.get_provider(srv["provider_id"])
            ptype = ((provider or {}).get("provider_type") or "").lower()
            if ptype not in config.LIBRARY_SERVER_TYPES:
                continue
            key = provider.get("password") or ""
            srv = {**srv, "type": ptype, "name": provider.get("name") or ptype.title(),
                   "url": (provider.get("base_url") or "").rstrip("/"), "has_key": bool(key),
                   "native_api_paths": bool(provider.get("native_api_paths"))}
        else:
            srv = {**srv, "provider_id": None}
            key = (config.get_library_server_key(srv["id"]) or "") if with_keys else ""
        if with_keys:
            srv["key"] = key
        out.append(srv)
    return out


def forget_provider(provider_id: int) -> None:
    """A deleted provider's titles stop counting right away."""
    vod_db.delete_library_presence(server=f"provider:{provider_id}")
    rebuild_matches()


async def scan_all() -> dict:
    """Scans every configured server. A server that fails keeps its previous rows."""
    async with _scan_lock:
        scan_start = time.time()
        servers = await asyncio.to_thread(configured_servers, True)
        results = []
        for server in servers:
            key = server["key"]
            res = {"id": server["id"], "name": server["name"], "ok": False, "count": 0, "error": None}
            if not (server["url"] and key and server["libraries"]):
                res["error"] = "Not set up: needs a URL, a token / API key and at least one library."
            else:
                try:
                    rows = await fetch_server(server, key)
                    await asyncio.to_thread(vod_db.replace_library_presence, server["id"], server["name"], rows)
                    res.update(ok=True, count=len(rows))
                except Exception as exc:
                    res["error"] = _redact(server["type"], exc)
                    logger.warning("[library_presence] scan of %s failed: %s", server["name"], res["error"])
            results.append(res)
        keep = [s["id"] for s in servers]      # also drops rows of a server no longer configured
        # import marks are pruned one by one, never mass-cleared on a clean scan
        await asyncio.to_thread(vod_db.delete_library_presence, keep + [IMPORT_SERVER])
        await asyncio.to_thread(_prune_import_marks, scan_start)
        matched = await asyncio.to_thread(rebuild_matches)
        _last_scan.update(at=time.time(), servers=results)
        return {"servers": results, "matched": matched}


def status() -> dict:
    summary = vod_db.library_presence_summary()
    return {"last_scan_at": _last_scan["at"], "last_results": _last_scan["servers"], "rows": summary}


# ── matching (local only) ────────────────────────────────────────────────────

def _norm_id(v) -> str | None:
    v = str(v).strip().lower() if v not in (None, "") else ""
    return v or None


class _Index:
    """Presence entries for one kind, looked up TMDB -> IMDb -> title + year."""

    def __init__(self):
        self.by_tmdb = defaultdict(list)
        self.by_imdb = defaultdict(list)
        self.by_title = defaultdict(list)
        self._seen = set()

    def add(self, tmdb, imdb, title, year, payload) -> None:
        tmdb, imdb, norm = _norm_id(tmdb), _norm_id(imdb), normalize_title(title or "")
        key = (tmdb, imdb, norm, year, id(payload) if isinstance(payload, dict) else payload)
        if key in self._seen:
            return
        self._seen.add(key)
        entry = (tmdb, imdb, year, payload)
        if tmdb:
            self.by_tmdb[tmdb].append(entry)
        if imdb:
            self.by_imdb[imdb].append(entry)
        if norm:
            self.by_title[norm].append(entry)

    def find(self, tmdb, imdb, title, year) -> list:
        tmdb, imdb, year = _norm_id(tmdb), _norm_id(imdb), _int(year)
        found = list(self.by_tmdb.get(tmdb, [])) if tmdb else []
        if imdb:
            found += self.by_imdb.get(imdb, [])
        candidates = [e for e in self.by_title.get(normalize_title(title or ""), [])
                      if not (tmdb and e[0] and e[0] != tmdb) and not (imdb and e[1] and e[1] != imdb)]
        identities = {(e[0], e[1], e[2]) for e in candidates}
        for e in candidates:
            if year is not None and e[2] is not None:
                if abs(year - e[2]) <= 1:
                    found.append(e)
            elif len(identities) == 1:
                found.append(e)
        out, seen = [], set()
        for e in found:
            k = id(e[3]) if isinstance(e[3], dict) else e[3]
            if k not in seen:
                seen.add(k)
                out.append(e[3])
        return out


def _label(row: dict) -> str:
    return f"{row['server_name']}: {row['library']}" if row.get("library") else row["server_name"]


def _join(labels) -> str:
    return ", ".join(dict.fromkeys(labels))


# serializes rebuild_matches' read->replace with mark_imported's writes so a mark isn't lost
_match_lock = threading.RLock()
IMPORT_MARK_TTL = 24 * 3600


def _indexes(rows: list[dict]):
    movies, shows, ep_groups = _Index(), _Index(), {}
    for r in rows:
        if r["kind"] == "movie":
            movies.add(r["tmdb_id"], r["imdb_id"], r["title"], r["year"], _label(r))
        elif r["kind"] in ("show", "episode"):
            shows.add(r["tmdb_id"], r["imdb_id"], r["title"], r["year"], _label(r))
        if r["kind"] == "episode" and r["season"] is not None and r["episode"] is not None:
            gkey = (r["server"], r["library"], r["tmdb_id"], r["imdb_id"], r["title"], r["year"])
            ep_groups.setdefault(gkey, {"label": _label(r), "eps": set(), "row": r})["eps"].add((r["season"], r["episode"]))
    episodes_index = _Index()
    for g in ep_groups.values():
        r = g["row"]
        episodes_index.add(r["tmdb_id"], r["imdb_id"], r["title"], r["year"], g)
    return movies, shows, episodes_index


def _prune_import_marks(scan_start: float) -> None:
    """Drops import marks a server row now covers, or older than a day. Marks made during the scan stay."""
    with _match_lock:
        rows = vod_db.list_library_presence()
        marks = [r for r in rows if r["server"] == IMPORT_SERVER]
        if not marks:
            return
        movies, shows, episodes = _indexes([r for r in rows if r["server"] != IMPORT_SERVER])
        now, drop = time.time(), []
        for m in marks:
            try:
                seen = float(m["seen_at"])
            except (TypeError, ValueError):
                seen = 0.0
            if seen >= scan_start:
                continue
            key = (m["tmdb_id"], m["imdb_id"], m["title"], m["year"])
            if m["kind"] == "movie":
                covered = bool(movies.find(*key))
            elif m["kind"] == "show":
                covered = bool(shows.find(*key))
            else:
                covered = any((m["season"], m["episode"]) in g["eps"] for g in episodes.find(*key))
            if covered or now - seen > IMPORT_MARK_TTL:
                drop.append(m["id"])
        vod_db.delete_library_presence_ids(drop)


def rebuild_matches() -> dict:
    """Recomputes library_presence_items from library_presence and the catalog."""
    with _match_lock:
        return _rebuild_matches()


def _rebuild_matches() -> dict:
    rows = vod_db.list_library_presence()
    catalog = vod_db.presence_match_inputs()
    movies, shows, episodes_index = _indexes(rows)

    items: list[tuple[str, int, str]] = []
    for m in catalog["movies"]:
        labels = movies.find(m["tmdb_id"], m["imdb_id"], m["name"], m["year"])
        if labels:
            items.append(("movie", m["id"], _join(labels)))
    eps_by_series = defaultdict(list)
    for e in catalog["episodes"]:
        eps_by_series[e["series_id"]].append(e)
    for s in catalog["series"]:
        labels = shows.find(s["tmdb_id"], s["imdb_id"], s["name"], s["year"])
        if labels:
            items.append(("series", s["id"], _join(labels)))
        groups = episodes_index.find(s["tmdb_id"], s["imdb_id"], s["name"], s["year"])
        if not groups:
            continue
        for e in eps_by_series.get(s["id"], []):
            se = (e["season_number"], e["episode_number"])
            ep_labels = [g["label"] for g in groups if se in g["eps"]]
            if ep_labels:
                items.append(("episode", e["id"], _join(ep_labels)))
    vod_db.replace_presence_items(items)
    counts = defaultdict(int)
    for kind, _, _ in items:
        counts[kind] += 1
    return dict(counts)


# ── after a Radarr/Sonarr import ─────────────────────────────────────────────

def mark_imported(job: dict) -> None:
    """Marks the item present right away; kept until a server lists it or it is a day old."""
    with _match_lock:
        _mark_imported(job)


def _mark_imported(job: dict) -> None:
    if job["kind"] == "movie":
        movie = vod_db.get_movie(job["item_id"])
        if not movie:
            return
        vod_db.add_library_presence_rows(IMPORT_SERVER, "Radarr", [{
            "kind": "movie", "library": _IMPORT_LIBRARY, "tmdb_id": movie.get("tmdb_id"),
            "imdb_id": movie.get("imdb_id"), "title": movie["name"], "year": movie.get("year")}])
        vod_db.add_presence_items([("movie", movie["id"], f"Radarr: {_IMPORT_LIBRARY}")])
        return
    episode = vod_db.get_episode(job["item_id"])
    series = vod_db.get_series(episode["series_id"]) if episode else None
    if not series:
        return
    base = {"library": _IMPORT_LIBRARY, "tmdb_id": series.get("tmdb_id"), "imdb_id": series.get("imdb_id"),
            "title": series["name"], "year": series.get("year")}
    vod_db.add_library_presence_rows(IMPORT_SERVER, "Sonarr", [
        {**base, "kind": "show"},
        {**base, "kind": "episode", "season": episode["season_number"], "episode": episode["episode_number"]}])
    label = f"Sonarr: {_IMPORT_LIBRARY}"
    vod_db.add_presence_items([("series", series["id"], label), ("episode", episode["id"], label)])


# ── the queue dialog's check ─────────────────────────────────────────────────

def already_present(movie_ids: list[int], series_ids: list[int], episode_ids: list[int],
                    season: int | None = None) -> list[dict]:
    out = []
    movie_labels = vod_db.presence_labels("movie", movie_ids)
    for mid in movie_ids:
        if mid in movie_labels:
            movie = vod_db.get_movie(mid) or {}
            out.append({"kind": "movie", "item_id": mid, "where": movie_labels[mid],
                        "title": strm_export._title_year(movie.get("name") or "", movie.get("year"))})

    def _episodes(series: dict, eps: list[dict]) -> list[dict]:
        labels = vod_db.presence_labels("episode", [e["id"] for e in eps])
        return [{"kind": "episode", "item_id": e["id"], "where": labels[e["id"]],
                 "title": f"{strm_export._base_title(series['name'])} "
                          f"S{int(e['season_number']):02d}E{int(e['episode_number']):02d}"}
                for e in eps if e["id"] in labels]

    for eid in episode_ids:
        episode = vod_db.get_episode(eid)
        series = vod_db.get_series(episode["series_id"]) if episode else None
        if series:
            out.extend(_episodes(series, [episode]))
    series_labels = vod_db.presence_labels("series", series_ids)
    for sid in series_ids:
        series = vod_db.get_series(sid)
        if not series:
            continue
        eps = [e for e in vod_db.list_episodes(sid) if season is None or int(e["season_number"]) == season]
        found = _episodes(series, eps)
        if not found and not eps and sid in series_labels:
            # no episodes in the catalog yet to compare; the show itself is there
            found = [{"kind": "series", "item_id": sid, "where": series_labels[sid],
                      "title": strm_export._title_year(series["name"], series.get("year"))}]
        out.extend(found)
    return out


# ── schedule ─────────────────────────────────────────────────────────────────

def _last_scan_epoch() -> float | None:
    if _last_scan["at"]:
        return _last_scan["at"]
    seen = [v["seen_at"] for k, v in vod_db.library_presence_summary().items() if k != IMPORT_SERVER and v["seen_at"]]
    if not seen:
        return None
    try:
        dt = datetime.fromisoformat(max(seen))
        return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()
    except ValueError:
        return None


async def scan_loop() -> None:
    """Scans every scan_hours; between scans, re-matches locally when the catalog changes."""
    fingerprint = None
    while True:
        await asyncio.sleep(_LOOP_SECS)
        try:
            settings = await asyncio.to_thread(config.get_library_presence_settings)
            last = await asyncio.to_thread(_last_scan_epoch)
            if await asyncio.to_thread(configured_servers) and (last is None or time.time() - last >= settings["scan_hours"] * 3600):
                await scan_all()
            current = await asyncio.to_thread(vod_db.presence_catalog_fingerprint)
            if current != fingerprint:
                await asyncio.to_thread(rebuild_matches)
                fingerprint = current
        except Exception:
            logger.exception("[library_presence] tick failed")
