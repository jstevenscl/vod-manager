"""Small Radarr/Sonarr v3 API client for the library handoff (see arr_handoff.py).

Only the calls the handoff needs: look a title up, add it without searching,
ask for an import of a finished file, and confirm the import took."""

import re

import httpx

_LABEL = {"radarr": "Radarr", "sonarr": "Sonarr"}
_IMPORT_COMMAND = {"radarr": "DownloadedMoviesScan", "sonarr": "DownloadedEpisodesScan"}
_RESOURCE = {"radarr": "movie", "sonarr": "series"}
# what Radarr/Sonarr say when they can't tell which title a file is from its folder/file name
_UNKNOWN_TITLE = {"Unknown Movie", "Unknown Series"}


class ArrError(Exception):
    pass


def _norm(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (title or "").lower())


class ArrClient:
    def __init__(self, kind: str, base_url: str, api_key: str, timeout: float = 15, transport=None):
        if kind not in _LABEL:
            raise ValueError("kind must be radarr or sonarr")
        self.kind = kind
        self.label = _LABEL[kind]
        self._http = httpx.Client(base_url=base_url.rstrip("/") + "/api/v3", timeout=timeout,
                                  headers={"X-Api-Key": api_key}, transport=transport)

    def _call(self, method: str, path: str, **kw):
        try:
            resp = self._http.request(method, path, **kw)
        except httpx.HTTPError as exc:
            raise ArrError(f"{self.label}: can't connect ({exc.__class__.__name__}: {exc})") from exc
        if resp.status_code in (401, 403):
            raise ArrError(f"{self.label}: {resp.status_code} Unauthorized — check the API key")
        if resp.status_code >= 400:
            raise ArrError(f"{self.label}: {resp.status_code} {resp.text[:200]}")
        return resp.json() if resp.content else None

    def system_status(self) -> dict:
        return self._call("GET", "/system/status")

    def root_folders(self) -> list[dict]:
        return [{"id": r["id"], "path": r["path"]} for r in self._call("GET", "/rootfolder") or []]

    def quality_profiles(self) -> list[dict]:
        return [{"id": p["id"], "name": p["name"]} for p in self._call("GET", "/qualityprofile") or []]

    def lookup(self, tmdb_id: int | None, title: str, year: int | None) -> dict | None:
        """The matching Radarr/Sonarr item (has an 'id' when already in the library), or
        None. Without a TMDB id only a single exact title (+ year) match counts."""
        term = f"tmdb:{tmdb_id}" if tmdb_id else title
        results = self._call("GET", f"/{_RESOURCE[self.kind]}/lookup", params={"term": term}) or []
        if tmdb_id:
            return results[0] if results else None
        hits = [r for r in results if _norm(r.get("title")) == _norm(title)
                and (year is None or r.get("year") == year)]
        return hits[0] if len(hits) == 1 else None

    def get_existing(self, tmdb_id: int | None, title: str, year: int | None) -> dict | None:
        item = self.lookup(tmdb_id, title, year)
        return item if item and item.get("id") else None

    def add(self, item: dict, root_folder: str, profile_id: int, monitor: str) -> dict:
        """Adds a looked-up item. Never starts a search."""
        body = {**item, "rootFolderPath": root_folder, "qualityProfileId": profile_id}
        if self.kind == "radarr":
            body.update(monitored=monitor == "monitored", addOptions={"searchForMovie": False})
        else:
            body.update(monitored=True, seasonFolder=True,
                        addOptions={"monitor": monitor, "searchForMissingEpisodes": False,
                                    "searchForCutoffUnmetEpisodes": False})
        return self._call("POST", f"/{_RESOURCE[self.kind]}", json=body)

    def import_path(self, path: str, arr_id: int | None = None, season: int | None = None,
                    episode: int | None = None) -> int:
        """Starts the import of a finished download. Radarr/Sonarr normally work out the title from the folder and
        file names; when they can't (a different title or year from their own record, the TMDB id tag is not read)
        and we already know which item this is (arr_id), the file is imported straight onto that item instead."""
        if arr_id:
            forced = self._import_known_item(path, arr_id, season, episode)
            if forced is not None:
                return forced
        cmd = self._call("POST", "/command", json={"name": _IMPORT_COMMAND[self.kind], "path": path,
                                                   "importMode": "Move"})
        return int(cmd["id"])

    def _import_known_item(self, path: str, arr_id: int, season: int | None, episode: int | None) -> int | None:
        """A ManualImport command for files Radarr/Sonarr could not match to a title, or None when the normal scan
        should run (the names matched, or the files are refused for some other reason the scan will report)."""
        movie = self.kind == "radarr"
        folder = path if movie else path.rsplit("/", 1)[0]
        entries = self._call("GET", "/manualimport", params={"folder": folder, "filterExistingFiles": "false"}) or []
        if not movie:
            entries = [e for e in entries if e.get("path") == path]
        reasons = {r.get("reason") for e in entries for r in e.get("rejections") or []}
        if not entries or not reasons or not reasons <= _UNKNOWN_TITLE:
            return None
        ids: dict = {"movieId": arr_id}
        if not movie:
            episode_id = next((e["id"] for e in self._call("GET", "/episode", params={"seriesId": arr_id}) or []
                               if e.get("seasonNumber") == season and e.get("episodeNumber") == episode), None)
            if episode_id is None:
                return None
            ids = {"seriesId": arr_id, "episodeIds": [episode_id]}
        files = [{"path": e["path"], **ids, "quality": e["quality"], "languages": e["languages"],
                  "releaseGroup": e.get("releaseGroup") or "", "indexerFlags": e.get("indexerFlags", 0)} for e in entries]
        cmd = self._call("POST", "/command", json={"name": "ManualImport", "files": files, "importMode": "move"})
        return int(cmd["id"])

    def command(self, cmd_id: int) -> dict:
        return self._call("GET", f"/command/{cmd_id}")

    def has_file(self, arr_id: int, season: int | None = None, episode: int | None = None) -> bool:
        if self.kind == "radarr":
            return bool(self._call("GET", f"/movie/{arr_id}").get("hasFile"))
        for ep in self._call("GET", "/episode", params={"seriesId": arr_id}) or []:
            if ep.get("seasonNumber") == season and ep.get("episodeNumber") == episode:
                return bool(ep.get("hasFile"))
        return False

    def rejection_for(self, path: str) -> str | None:
        """Why Radarr/Sonarr won't import what's at path (its own rejection reasons), or None."""
        reasons = []
        for entry in self._call("GET", "/manualimport", params={"folder": path}) or []:
            reasons += [r.get("reason") for r in entry.get("rejections") or [] if r.get("reason")]
        return "; ".join(dict.fromkeys(reasons)) or None
