"""Small Radarr/Sonarr v3 API client for the library handoff (see arr_handoff.py).

Only the calls the handoff needs: look a title up, add it without searching,
ask for an import of a finished file, and confirm the import took."""

import re

import httpx

_LABEL = {"radarr": "Radarr", "sonarr": "Sonarr"}
_IMPORT_COMMAND = {"radarr": "DownloadedMoviesScan", "sonarr": "DownloadedEpisodesScan"}
_RESOURCE = {"radarr": "movie", "sonarr": "series"}


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

    def import_path(self, path: str) -> int:
        cmd = self._call("POST", "/command", json={"name": _IMPORT_COMMAND[self.kind], "path": path,
                                                   "importMode": "Move"})
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
