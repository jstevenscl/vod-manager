"""Opt-in downloads: save movies / episodes from a provider into a library folder.

Off by default (Configuration -> Downloads). Saving a provider's files to disk is a
different act from linking to them, so nothing here runs until the user turns it on
and picks a folder. When on, a background worker takes queued jobs and either

  * copies the original file byte-for-byte (resuming a partial file where the
    provider allows it), or
  * re-encodes it with ffmpeg to a smaller H.264 / H.265 file (presets below).

Nothing half-done ever touches the library: every download and encode happens in a processing folder
(Configuration -> Downloads, or a hidden .processing folder inside the library folder), and only a
finished file that passed the ffprobe check is moved into place.

Files land in the same Emby / Jellyfin / Plex naming the .strm export uses:

    Movies/Title (Year) [tmdbid-N]/Title (Year).ext
    Shows/Show (Year) [tmdbid-N]/Season NN/Show - SNNENN.ext

Provider connection limits are respected: a download takes a connection slot the same
way playback does (xc_server._select_upstream_credentials) and the gate works both ways
(a viewer can't start on a connection a download holds, and vice versa). When a provider
has no free slot the job waits and tries again instead of failing.

Providers differ, so the pacing is a global default that each provider can override
(Providers -> Downloads): a speed cap, how many downloads may run at once on that provider,
and a pause between downloads. If a provider pushes back (HTTP 429 / 458 / 509 / 403) the
job backs off automatically with a growing delay. A re-encode only holds the provider
connection while the file is being fetched; the CPU-heavy encode runs afterwards from the
local copy. Nothing is ever deleted from the library folder except our own partial files.
"""

import asyncio
import collections
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

import config
import strm_export
import vod_db
import xc_server

logger = logging.getLogger("vod_manager")

# id -> (label, ffmpeg video codec, crf, max height). "original" copies the file as-is.
PRESETS: dict[str, dict] = {
    "original":  {"label": "Original file (no re-encode)"},
    "h264_1080": {"label": "H.264, up to 1080p (smaller, plays everywhere)", "codec": "libx264", "crf": 23, "height": 1080},
    "h264_720":  {"label": "H.264, up to 720p (much smaller)", "codec": "libx264", "crf": 24, "height": 720},
    "h264_480":  {"label": "H.264, up to 480p (phone-sized)", "codec": "libx264", "crf": 26, "height": 480},
    "hevc_1080": {"label": "H.265, up to 1080p (smallest, needs a modern player)", "codec": "libx265", "crf": 27, "height": 1080},
    "hevc_720":  {"label": "H.265, up to 720p (tiny)", "codec": "libx265", "crf": 28, "height": 720},
}

MAX_QUEUE_PER_REQUEST = 2000
_CAPACITY_WAIT_SECONDS = 60
_PROGRESS_WRITE_SECONDS = 1.0
_SPACE_CHECK_SECONDS = 10.0
_MAX_ATTEMPTS = 3
# A provider's throughput can swing 10x from one connection to the next, so a transfer that has crawled for a
# while is dropped and resumed (Range) on a fresh connection. Judged over the last _SLOW_WINDOW seconds.
_SLOW_WINDOW = 30.0
_SLOW_FLOOR_BPS = 1_000_000          # below ~1 MB/s ...
_SLOW_FRACTION = 0.2                 # ... or below a fifth of the best this file has managed
_SLOW_MIN_REMAINING = 200 * 1024 * 1024
_SLOW_MAX_RECONNECTS = 4
_SPEED_WINDOW = 8.0                  # the live MB/s shown in the queue
_WRITE_CHUNK = 1024 * 1024

_PUSHBACK_STATUSES = (403, 429, 458, 509)
_PUSHBACK_BASE_SECONDS = 120
_PUSHBACK_MAX_SECONDS = 1800
_PUSHBACK_GIVE_UP = 6

_running: dict[int, asyncio.Task] = {}
_cancel_ids: set[int] = set()
_provider_active: dict[int, int] = {}      # provider_id -> downloads currently transferring from it
_cooldown_until: dict[int, float] = {}     # provider_id -> epoch seconds before the next download may start
_pushbacks: dict[int, int] = {}            # provider_id -> consecutive "slow down" answers
_job_provider: dict[int, int] = {}         # job_id -> provider whose connection it holds right now
_preempt_ids: set[int] = set()             # jobs asked to step aside for a viewer


class DownloadError(Exception):
    """A failure worth reporting on the job (message is already credential-safe)."""


class _Cancelled(Exception):
    pass


class _Preempted(Exception):
    """A viewer needs this download's provider connection: stop now, keep the partial file, resume later."""


class _Pushback(Exception):
    """The provider answered with a rate-limit / ban style status."""

    def __init__(self, provider_name: str, status: int):
        self.provider_name = provider_name
        self.status = status


class _AtCapacity(Exception):
    def __init__(self, provider_name: str, why: str, wait: int = 0):
        self.provider_name = provider_name
        self.why = why
        self.wait = wait


def preempt_provider_downloads(provider_ids) -> bool:
    """Asks every download holding a connection on these providers to pause. True if any was signalled."""
    jobs = [j for j, p in _job_provider.items() if p in provider_ids]
    _preempt_ids.update(jobs)
    return bool(jobs)


xc_server._download_preempt_hook = preempt_provider_downloads


def _safe(text: str) -> str:
    """Upstream errors and ffmpeg output can embed the provider's real login in a URL."""
    return xc_server._redact_upstream_url(str(text))


# ── settings ────────────────────────────────────────────────────────────────

def _dir_problem(label: str, value: str, strm_dir: str) -> str | None:
    path = Path(value)
    if not path.is_absolute():
        return f"The {label} must be an absolute path."
    if path.parent == path:
        return f"The {label} cannot be a filesystem root."
    if strm_dir:
        try:
            if Path(strm_dir).resolve() == path.resolve():
                return f"Use a different folder for the {label} than the .strm export, or the library would list every title twice."
        except OSError:
            pass
    return None


def configured_dirs(settings: dict) -> list[tuple[str, str]]:
    """(label, path) for every folder the user typed in."""
    return [(label, (settings.get(key) or "").strip())
            for label, key in (("download folder", "output_dir"), ("movies folder", "movies_dir"), ("TV shows folder", "shows_dir"),
                               ("processing folder", "work_dir"))
            if (settings.get(key) or "").strip()]


def roots_for(settings: dict) -> dict[str, Path]:
    """Where each kind of file goes: {'movie': root, 'episode': root}. A dedicated movies / TV shows folder is
    used as is; otherwise the shared download folder gets Movies/ and Shows/ subfolders."""
    base = (settings.get("output_dir") or "").strip()
    movies = (settings.get("movies_dir") or "").strip()
    shows = (settings.get("shows_dir") or "").strip()
    return {
        "movie": Path(movies) if movies else Path(base) / "Movies",
        "episode": Path(shows) if shows else Path(base) / "Shows",
    }


def work_root_for(settings: dict, library_root: Path) -> Path:
    """Where in-progress files live: the processing folder if one is set, else a hidden folder next to the
    library files (same filesystem, so the final move is an instant rename)."""
    wd = (settings.get("work_dir") or "").strip()
    return Path(wd) if wd else library_root / ".processing"


def validate_settings(settings: dict) -> str | None:
    base = (settings.get("output_dir") or "").strip()
    movies = (settings.get("movies_dir") or "").strip()
    shows = (settings.get("shows_dir") or "").strip()
    if not base and not (movies and shows):
        return "Choose a download folder (or set both a movies folder and a TV shows folder)."
    strm_dir = (config.get_strm_export_settings().get("output_dir") or "").strip()
    for label, value in configured_dirs(settings):
        problem = _dir_problem(label, value, strm_dir)
        if problem:
            return problem
    try:
        r = roots_for(settings)
        if r["movie"].resolve() == r["episode"].resolve():
            return "Use different folders for movies and TV shows."
        work = (settings.get("work_dir") or "").strip()
        if work and Path(work).resolve() in (r["movie"].resolve(), r["episode"].resolve()):
            return "The processing folder must be different from the movies and TV shows folders, or unfinished files would sit in your library."
    except OSError:
        pass
    preset = settings.get("default_preset", "original")
    if preset not in PRESETS:
        return "Unknown default re-encode preset."
    return None


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def in_window(settings: dict, now: float | None = None) -> bool:
    """True when downloads may START now (always, unless a time window is configured)."""
    if not settings.get("window_enabled"):
        return True
    hour = time.localtime(now if now is not None else time.time()).tm_hour
    start, end = int(settings["window_start"]), int(settings["window_end"])
    if start == end:
        return True
    return start <= hour < end if start < end else (hour >= start or hour < end)


def provider_policy(provider: dict, settings: dict) -> dict:
    """Pacing for one provider: its own override where set, else the global default."""
    def pick(key: str, default):
        v = provider.get(key)
        return default if v is None else v
    return {
        "speed_mbps": max(0, int(pick("download_speed_limit_mbps", settings.get("speed_limit_mbps", 0)) or 0)),
        "max_concurrent": min(4, max(1, int(pick("download_max_concurrent", settings.get("provider_concurrent", 1)) or 1))),
        "pause_seconds": max(0, int(pick("download_pause_seconds", settings.get("pause_seconds", 0)) or 0)),
    }


async def _validate_media(path: Path) -> None:
    """ffprobe sanity check on a finished download: a provider error body saved as .mp4 or a
    truncated file is caught here rather than discovered by the media server later."""
    if not shutil.which("ffprobe"):
        return
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), 90)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise DownloadError("The downloaded file couldn't be checked (ffprobe timed out).")
    if proc.returncode != 0 or not out.strip():
        raise DownloadError("The downloaded file isn't a playable video (it failed the ffprobe check).")


# ── audio / subtitle languages ──────────────────────────────────────────────

_LANG_ALIASES: dict[str, set[str]] = {
    "en": {"en", "eng", "english"}, "es": {"es", "spa", "spanish", "espanol", "español"}, "fr": {"fr", "fre", "fra", "french"},
    "de": {"de", "ger", "deu", "german"}, "it": {"it", "ita", "italian"}, "pt": {"pt", "por", "portuguese"},
    "ru": {"ru", "rus", "russian"}, "ja": {"ja", "jpn", "japanese"}, "ko": {"ko", "kor", "korean"},
    "zh": {"zh", "chi", "zho", "cmn", "chinese", "mandarin"}, "ar": {"ar", "ara", "arabic"}, "hi": {"hi", "hin", "hindi"},
    "nl": {"nl", "dut", "nld", "dutch"}, "sv": {"sv", "swe", "swedish"}, "no": {"no", "nor", "nob", "norwegian"},
    "da": {"da", "dan", "danish"}, "fi": {"fi", "fin", "finnish"}, "pl": {"pl", "pol", "polish"}, "tr": {"tr", "tur", "turkish"},
    "el": {"el", "gre", "ell", "greek"}, "he": {"he", "heb", "hebrew"}, "th": {"th", "tha", "thai"},
    "cs": {"cs", "cze", "ces", "czech"}, "hu": {"hu", "hun", "hungarian"}, "ro": {"ro", "rum", "ron", "romanian"},
    "uk": {"uk", "ukr", "ukrainian"}, "vi": {"vi", "vie", "vietnamese"}, "id": {"id", "ind", "indonesian"},
    "fa": {"fa", "per", "fas", "persian"}, "ur": {"ur", "urd", "urdu"}, "bg": {"bg", "bul", "bulgarian"},
    "hr": {"hr", "hrv", "croatian"}, "sr": {"sr", "srp", "serbian"}, "sk": {"sk", "slo", "slk", "slovak"},
}
_ALIAS_TO_KEY = {alias: key for key, aliases in _LANG_ALIASES.items() for alias in aliases}
_UNKNOWN_LANGS = {"", "und", "unk", "zxx", "mis", "mul", "undetermined"}
_TEXT_SUB_CONVERT = {"mov_text", "webvtt", "text"}   # not allowed in matroska as-is: converted to srt


def lang_key(value: str | None) -> str:
    v = (value or "").strip().lower()
    return _ALIAS_TO_KEY.get(v, v)


def parse_languages(text: str | None) -> list[str]:
    """'eng, Japanese' -> ['en', 'ja']. Empty, 'all' or '*' mean "keep every track" and give []."""
    out: list[str] = []
    for part in re.split(r"[,\s;/]+", (text or "").strip().lower()):
        if not part or part in ("all", "*", "any"):
            return []
        k = lang_key(part)
        if k not in out:
            out.append(k)
    return out


def languages_for(job: dict, settings: dict) -> list[str]:
    raw = job.get("audio_languages")
    return parse_languages(settings.get("audio_languages") if raw is None else raw)


@dataclass
class TrackPlan:
    """Which streams of the source go into the output (indexes are the source's own stream numbers)."""
    video: int | None
    audio: list[int] = field(default_factory=list)
    subs: list[tuple[int, str]] = field(default_factory=list)   # (stream index, codec)
    changed: bool = False                                       # differs from "every stream as is"
    src_height: int | None = None                               # the source video's height, when the probe knew it

    def without_subs(self) -> "TrackPlan":
        return TrackPlan(self.video, self.audio, [], True, self.src_height)


def _matching(tracks: list[dict], langs: list[str]) -> list[dict]:
    # a track with no (or unknown) language tag can't be ruled out, so it is kept
    return [t for t in tracks if (t.get("language") or "").strip().lower() in _UNKNOWN_LANGS or lang_key(t.get("language")) in langs]


def plan_tracks(streams: list[dict], langs: list[str], keep_subs: bool) -> TrackPlan:
    """Picks the tracks to keep. With no language filter every audio track stays; with one, audio in those
    languages (plus untagged tracks) stays, falling back to ALL audio when nothing matches so a file is never
    left silent. Subtitles are kept (in the same languages) only when keep_subs is on."""
    videos = [s for s in streams if s.get("codec_type") == "video" and not s.get("attached_pic")]
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    subs = [s for s in streams if s.get("codec_type") == "subtitle"]
    audio_sel = (_matching(audio, langs) or audio) if langs else audio
    if not keep_subs:
        subs_sel: list[dict] = []
    else:
        subs_sel = _matching(subs, langs) if langs else subs
    changed = ({a["index"] for a in audio_sel} != {a["index"] for a in audio}
               or {s["index"] for s in subs_sel} != {s["index"] for s in subs})
    return TrackPlan(
        video=videos[0]["index"] if videos else None,
        audio=[a["index"] for a in audio_sel],
        subs=[(s["index"], (s.get("codec_name") or "").lower()) for s in subs_sel],
        changed=changed,
        src_height=videos[0].get("height") if videos else None)


async def _probe_streams(path: Path | str) -> list[dict] | None:
    """Streams of a local media file (index, type, codec, language tag), or None if it can't be read."""
    if not shutil.which("ffprobe"):
        return None
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-show_entries", "stream=index,codec_type,codec_name,height:stream_tags=language:stream_disposition=attached_pic",
        "-of", "json", str(path), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), 90)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return None
    if proc.returncode != 0:
        return None
    try:
        raw = json.loads(out.decode(errors="replace")).get("streams") or []
    except ValueError:
        return None
    return [{"index": int(r["index"]), "codec_type": r.get("codec_type"), "codec_name": r.get("codec_name"),
             "height": int(r["height"]) if r.get("height") else None,
             "language": (r.get("tags") or {}).get("language"), "attached_pic": bool((r.get("disposition") or {}).get("attached_pic"))}
            for r in raw if "index" in r]


def _free_bytes(path: Path) -> int | None:
    probe = path
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    try:
        return shutil.disk_usage(probe).free
    except OSError:
        return None


# ── naming ──────────────────────────────────────────────────────────────────

def target_for(job: dict) -> tuple[str, str, int | None]:
    """(folder relative to the movies / TV shows root, file stem, duration) for the job's item.
    Raises DownloadError when the item no longer exists."""
    # KNM: 2026-10-18 the [tmdbid-N] folder tag can be turned off in Configuration -> Downloads
    tag = config.get_download_settings()["tmdb_id_in_folder"]
    if job["kind"] == "movie":
        movie = vod_db.get_movie(job["item_id"])
        if not movie:
            raise DownloadError("This movie no longer exists.")
        title = strm_export._title_year(movie["name"], movie.get("year"))
        folder = f"{title} [tmdbid-{movie['tmdb_id']}]" if tag and movie.get("tmdb_id") else title
        return folder, title, movie.get("duration_secs")
    episode = vod_db.get_episode(job["item_id"])
    series = vod_db.get_series(episode["series_id"]) if episode else None
    if not episode or not series:
        raise DownloadError("This episode no longer exists.")
    show_title = strm_export._title_year(series["name"], series.get("year"))
    show_dir = f"{show_title} [tmdbid-{series['tmdb_id']}]" if tag and series.get("tmdb_id") else show_title
    season, number = int(episode["season_number"]), int(episode["episode_number"])
    stem = f"{strm_export._base_title(series['name'])} - S{season:02d}E{number:02d}"
    return f"{show_dir}/Season {season:02d}", stem, episode.get("duration_secs")


def _existing_file(folder: Path, stem: str) -> Path | None:
    """A finished file for this stem already in the folder (a .strm stub or a half-written
    .part does not count)."""
    if not folder.is_dir():
        return None
    for entry in folder.iterdir():
        name = entry.name
        if (name.startswith(stem + ".") and entry.is_file()
                and not name.lower().endswith((".part", ".src", ".strm", ".nfo", ".jpg", ".png", ".srt"))):
            return entry
    return None


def _extension(preset: str, source: dict) -> str:
    if preset != "original":
        return "mkv"
    ext = ""
    if source.get("local_file_path"):
        ext = Path(source["local_file_path"]).suffix.lstrip(".")
    ext = re.sub(r"[^A-Za-z0-9]", "", ext or source.get("container_extension") or "") or "mp4"
    return ext.lower()


# ── queueing ────────────────────────────────────────────────────────────────

def _ordered_sources(job: dict) -> list[dict]:
    explicit = job.get("source_id")
    if explicit:
        getter = vod_db.get_movie_source_for_streaming if job["kind"] == "movie" else vod_db.get_episode_source_for_streaming
        src = getter(explicit)
        return [src] if src else []
    lister = vod_db.list_movie_sources_for_streaming if job["kind"] == "movie" else vod_db.list_episode_sources_for_streaming
    sources = lister(job["item_id"])
    prefer = job.get("prefer_provider_id")
    if prefer:
        sources.sort(key=lambda s: 0 if s["provider_id"] == prefer else 1)  # stable: keeps priority order otherwise
    return sources


def enqueue(movie_ids: list[int] | None = None, series_ids: list[int] | None = None,
            episode_ids: list[int] | None = None, source_id: int | None = None,
            prefer_provider_id: int | None = None, preset: str | None = None,
            season: int | None = None, audio_languages: str | None = None) -> dict:
    """Queues titles for download. A series queues every episode that has a playable source
    (optionally just one season). source_id pins one exact source and is only valid for a
    single movie or episode. Raises ValueError for a bad request."""
    movie_ids = list(dict.fromkeys(movie_ids or []))
    series_ids = list(dict.fromkeys(series_ids or []))
    episode_ids = list(dict.fromkeys(episode_ids or []))
    preset = preset or config.get_download_settings()["default_preset"]
    if preset not in PRESETS:
        raise ValueError("Unknown preset.")
    if preset != "original" and not ffmpeg_available():
        raise ValueError("Re-encoding needs ffmpeg, which isn't available in this install.")
    if audio_languages is not None:
        audio_languages = audio_languages.strip().lower() or None     # blank = follow the setting
        if audio_languages and parse_languages(audio_languages) and not ffmpeg_available():
            raise ValueError("Choosing audio languages needs ffmpeg, which isn't available in this install.")
    if source_id is not None and (series_ids or len(movie_ids) + len(episode_ids) != 1):
        raise ValueError("Choosing one exact source works for a single movie or episode.")
    if not (movie_ids or series_ids or episode_ids):
        raise ValueError("Nothing selected.")

    result = {"queued": 0, "already_queued": 0, "no_source": 0, "missing": 0, "truncated": False, "preset": preset}
    # library handoff: new jobs are added to Radarr/Sonarr by arr_handoff.resolve_pending
    handoff = {k: config.handoff_enabled(k) for k in ("movie", "episode")}

    def add(kind: str, item_id: int, title: str, src: int | None = None) -> bool:
        if result["queued"] >= MAX_QUEUE_PER_REQUEST:
            result["truncated"] = True
            return False
        jid = vod_db.enqueue_download(kind, item_id, title, preset, src, prefer_provider_id, audio_languages)
        if jid and handoff[kind]:
            vod_db.update_import(jid, import_status="add")
        result["queued" if jid else "already_queued"] += 1
        return True

    for mid in movie_ids:
        movie = vod_db.get_movie(mid)
        if not movie:
            result["missing"] += 1
            continue
        if source_id is not None:
            src = vod_db.get_movie_source_for_streaming(source_id)
            if not src or src["movie_id"] != mid:
                raise ValueError("That source doesn't belong to this movie (or its provider is inactive).")
        elif not vod_db.list_movie_sources_for_streaming(mid):
            result["no_source"] += 1
            continue
        if not add("movie", mid, strm_export._title_year(movie["name"], movie.get("year")), source_id):
            break

    for eid in episode_ids:
        episode = vod_db.get_episode(eid)
        series = vod_db.get_series(episode["series_id"]) if episode else None
        if not episode or not series:
            result["missing"] += 1
            continue
        if source_id is not None:
            src = vod_db.get_episode_source_for_streaming(source_id)
            if not src or src["episode_id"] != eid:
                raise ValueError("That source doesn't belong to this episode (or its provider is inactive).")
        elif not vod_db.list_episode_sources_for_streaming(eid):
            result["no_source"] += 1
            continue
        label = f"{strm_export._base_title(series['name'])} S{int(episode['season_number']):02d}E{int(episode['episode_number']):02d}"
        if not add("episode", eid, label, source_id):
            break

    for sid in series_ids:
        series = vod_db.get_series(sid)
        if not series:
            result["missing"] += 1
            continue
        for episode in vod_db.list_episodes(sid):
            if season is not None and int(episode["season_number"]) != season:
                continue
            if not vod_db.list_episode_sources_for_streaming(episode["id"]):
                result["no_source"] += 1
                continue
            label = f"{strm_export._base_title(series['name'])} S{int(episode['season_number']):02d}E{int(episode['episode_number']):02d}"
            if not add("episode", episode["id"], label):
                break
        if result["truncated"]:
            break
    return result


# ── "Save to this device": short-lived link to one finished file ────────────
# The admin API authenticates with a header a plain browser link can't send, so a click on Save asks for
# a link that carries its own unguessable, expiring token and unlocks exactly that one file.
_FILE_LINK_SECONDS = 15 * 60
_file_tokens: dict[str, tuple[int, float]] = {}


def issue_file_link(job_id: int) -> str | None:
    now = time.time()
    for t in [t for t, (_, exp) in _file_tokens.items() if exp < now]:
        _file_tokens.pop(t, None)
    if file_for_job(job_id) is None:
        return None
    token = secrets.token_urlsafe(24)
    _file_tokens[token] = (job_id, now + _FILE_LINK_SECONDS)
    return token


def file_for_job(job_id: int) -> Path | None:
    """The finished file of a done job, only if it really sits inside the download folder."""
    job = vod_db.get_download_job(job_id)
    settings = config.get_download_settings()
    if not job or job["status"] != "done" or not job.get("final_path") or validate_settings(settings):
        return None
    root = roots_for(settings)["movie" if job["kind"] == "movie" else "episode"].resolve()
    path = (root / job["final_path"]).resolve()
    if root != path and root not in path.parents:
        return None
    return path if path.is_file() else None


def resolve_file_link(token: str) -> Path | None:
    entry = _file_tokens.get(token)
    if not entry or entry[1] < time.time():
        _file_tokens.pop(token, None)
        return None
    return file_for_job(entry[0])


def purge_work(job_ids: list[int]) -> None:
    """Removes the leftover processing files of jobs that are being dropped from the list."""
    settings = config.get_download_settings()
    if validate_settings(settings):
        return
    roots = {work_root_for(settings, r) for r in roots_for(settings).values()}
    for root in roots:
        for jid in job_ids:
            shutil.rmtree(root / f"job-{jid}", ignore_errors=True)


def cancel(job_ids: list[int] | None = None) -> list[int]:
    ids = vod_db.cancel_download_jobs(job_ids)
    _cancel_ids.update(ids)
    return ids


def get_status() -> dict:
    settings = config.get_download_settings()
    typed = [d for d in configured_dirs(settings) if d[0] != "processing folder"]
    problem = validate_settings(settings) if settings["enabled"] or typed else None
    free = None
    if typed and problem is None:
        r = roots_for(settings)
        free = {"movies": _free_bytes(r["movie"]), "shows": _free_bytes(r["episode"]),
                "movies_path": str(r["movie"]), "shows_path": str(r["episode"])}
    return {
        "settings": settings,
        "problem": problem,
        "ffmpeg": ffmpeg_available(),
        "hardware": detect_hardware(),
        "in_window": in_window(settings),
        "free": free,
        "counts": vod_db.download_job_counts(),
        "jobs": vod_db.list_download_jobs(),
        "presets": [{"id": k, "label": v["label"]} for k, v in PRESETS.items()],
    }


# ── the transfer itself ─────────────────────────────────────────────────────

class _Ctx:
    """Per-job progress + cancel bookkeeping shared by both transfer paths."""

    def __init__(self, job_id: int, out_root: Path, min_free: int, session_id: str):
        self.job_id = job_id
        self.out_root = out_root
        self.min_free = min_free
        self.session_id = session_id
        self.rate_bps = 0           # 0 = unthrottled
        self.lo, self.hi = 0.0, 1.0  # this phase's slice of the 0..1 progress bar
        self.note: str | None = None
        self.reconnect_slow = True  # drop and resume a crawling transfer on a fresh connection
        self._last_write = 0.0
        self._last_space = 0.0

    def phase(self, lo: float, hi: float, note: str | None) -> None:
        self.lo, self.hi, self.note = lo, hi, note
        self._last_write = 0.0

    def cancelled(self) -> bool:
        return self.job_id in _cancel_ids or self.session_id in xc_server._kill_requested

    def check(self) -> None:
        if self.cancelled():
            raise _Cancelled()
        if self.job_id in _preempt_ids:
            raise _Preempted()

    def progress(self, done: int, total: int | None, fraction: float | None = None, force: bool = False) -> None:
        sess = xc_server._active_sessions.get(self.session_id)
        if sess is not None:
            sess["bytes_sent"] = done
            sess["total_bytes"] = total or 0
        now = time.monotonic()
        if force or now - self._last_write >= _PROGRESS_WRITE_SECONDS:
            self._last_write = now
            if fraction is None:
                fraction = (done / total) if total else 0.0
            fraction = self.lo + (self.hi - self.lo) * min(fraction, 1.0)
            vod_db.update_download_job(
                self.job_id, ("running",), bytes_done=int(done), total_bytes=total, progress=round(fraction, 4), note=self.note)
        if now - self._last_space >= _SPACE_CHECK_SECONDS:
            self._last_space = now
            free = _free_bytes(self.out_root)
            if free is not None and self.min_free and free < self.min_free:
                raise DownloadError("Stopped: free space on the download folder dropped below the limit you set.")


_CAPACITY_WORDS = ("max connection", "maximum", "too many", "connection limit", "limit reached", "already in use", "simultaneous")


def _looks_like_video(head: bytes) -> bool:
    """Some providers serve a real file labelled text/html. The first bytes tell the truth."""
    return (head[:4] == b"\x1a\x45\xdf\xa3"  # Matroska / WebM
            or head[4:8] == b"ftyp"  # MP4 / MOV
            or (head[:4] == b"RIFF" and head[8:12] == b"AVI ")
            or head[:4] == b"OggS" or head[:3] == b"FLV"
            or (len(head) >= 376 and head[0] == 0x47 and head[188] == 0x47 and head[376] == 0x47))  # MPEG-TS


async def _body_snippet(resp, head: bytes = b"") -> str:
    """First bit of an unexpected response, flattened to plain text, so the job error says what the provider said."""
    if head and sum(b < 9 or 13 < b < 32 for b in head[:512]) > 5:
        return ""  # binary, not a page: nothing readable to quote
    try:
        if head:
            chunks = [head]
        else:
            chunks = [c async for c in resp.aiter_bytes()]
        for chunk in chunks:
            text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", chunk[:2000].decode("utf-8", "replace"), flags=re.S | re.I)
            text = re.sub(r"<[^>]+>", " ", text)
            return _safe(re.sub(r"\s+", " ", text).strip())[:160]
    except Exception:
        pass
    return ""


def _page_error(provider_name: str, status: int, ctype: str, snippet: str) -> Exception:
    """A provider that answers with a page instead of video is usually saying something: a connection limit
    (wait and retry) or an expired / blocked account (fail over to the next source)."""
    if snippet and any(w in snippet.lower() for w in _CAPACITY_WORDS):
        return _AtCapacity(provider_name, f'the provider says: "{snippet}"', wait=120)
    kind = (ctype.split(";")[0] or "text").strip()
    detail = f' It said: "{snippet}".' if snippet else ""
    return DownloadError(f"The provider returned a web page instead of a video file (HTTP {status}, {kind}).{detail}")


async def _prepend(first: bytes, rest):
    if first:
        yield first
    async for chunk in rest:
        yield chunk


def _server_id(host: str) -> str:
    """A short stable id for the server a download was redirected to: same id, same server. The host itself is not shown."""
    return hashlib.sha1((host or "").encode()).hexdigest()[:5]


def _window_rate(samples, window: float) -> float | None:
    """Bytes/second over the last `window` seconds of (time, bytes) samples, or None until there are enough."""
    if len(samples) < 2:
        return None
    now, cur = samples[-1]
    old = next(((t, b) for t, b in samples if now - t <= window), samples[0])
    span = now - old[0]
    return (cur - old[1]) / span if span >= min(window, 3.0) * 0.8 else None


async def _download_http(url: str, part: Path, user_agent: str | None, ctx: _Ctx, provider_name: str = "provider",
                         expected_total: int | None = None) -> int:
    """Streams url into part, resuming a previous partial file, at most ctx.rate_bps bytes/second
    when a speed cap applies. Returns the final size. The queue note shows the live speed and which server
    the provider redirected to; a transfer that crawls is resumed on a fresh connection (see _SLOW_*).
    A provider that sends no Content-Length leaves the progress bar without a total; expected_total (the size a
    Check quality found) stands in for it on the bar only, never for the free-space or completeness checks."""
    headers = {"User-Agent": user_agent} if user_agent else dict(xc_server._UPSTREAM_HEADERS)
    attempt, reconnects, peak = 0, 0, 0.0
    while attempt < _MAX_ATTEMPTS:
        attempt += 1
        ctx.check()
        resume = part.stat().st_size if part.exists() else 0
        req_headers = dict(headers)
        if resume:
            req_headers["Range"] = f"bytes={resume}-"
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=90.0), follow_redirects=True) as client:
                async with client.stream("GET", url, headers=req_headers) as resp:
                    if resp.status_code == 416 and resume:
                        part.unlink(missing_ok=True)  # the partial no longer lines up with the source: start over
                        continue
                    if resp.status_code in _PUSHBACK_STATUSES:
                        raise _Pushback(provider_name, resp.status_code)
                    if resp.status_code >= 400:
                        raise DownloadError(f"The provider answered HTTP {resp.status_code}.")
                    ctype = (resp.headers.get("content-type") or "").lower()
                    chunks = resp.aiter_bytes(256 * 1024)
                    first = b""
                    if ctype.startswith("text/") or "json" in ctype:
                        # Judge by the bytes, not the label: a mislabelled real file must still download.
                        first = await anext(chunks, b"")
                        if not _looks_like_video(first):
                            raise _page_error(provider_name, resp.status_code, ctype, await _body_snippet(resp, first))
                    if resp.status_code == 206 and resume:
                        mode, done = "ab", resume
                        total = None
                        cr = resp.headers.get("content-range", "")
                        if "/" in cr and cr.rsplit("/", 1)[-1].isdigit():
                            total = int(cr.rsplit("/", 1)[-1])
                    else:
                        mode, done = "wb", 0
                        total = int(resp.headers["content-length"]) if resp.headers.get("content-length", "").isdigit() else None
                    shown = total or expected_total
                    free = _free_bytes(ctx.out_root)
                    if total and free is not None and free - (total - done) < ctx.min_free:
                        raise DownloadError("Not enough free space for this file with the free-space limit you set.")
                    # Resuming needs the provider to honour Range. Some send a non-standard value (e.g. "0-123"), so
                    # anything except none counts; with no header at all a transfer is never dropped on purpose.
                    ar = (resp.headers.get("accept-ranges") or "").strip().lower()
                    can_resume = bool(ar) and ar != "none" and bool(total)
                    sid = _server_id(resp.url.host)
                    tag = f"server {sid}" + (f" · reconnected {reconnects}×" if reconnects else "")
                    ctx.note = f"Downloading · {tag}"
                    buf = bytearray()
                    began, began_done = time.monotonic(), done
                    samples: collections.deque = collections.deque()
                    last_note, slow = 0.0, False
                    with open(part, mode) as fh:
                        async for chunk in _prepend(first, chunks):
                            buf += chunk
                            now = time.monotonic()
                            samples.append((now, done + len(buf)))
                            while samples and now - samples[0][0] > _SLOW_WINDOW + 1:
                                samples.popleft()
                            if ctx.rate_bps:
                                # sleep long enough that the average since we began stays at the cap
                                ahead = (done + len(buf) - began_done) / ctx.rate_bps - (time.monotonic() - began)
                                if ahead > 0:
                                    await asyncio.sleep(min(ahead, 5))
                            if now - last_note >= 2:
                                last_note = now
                                rate = _window_rate(samples, _SPEED_WINDOW)
                                ctx.note = f"Downloading · {rate / 1e6:.1f} MB/s · {tag}" if rate is not None else f"Downloading · {tag}"
                            if (ctx.reconnect_slow and can_resume and not ctx.rate_bps and reconnects < _SLOW_MAX_RECONNECTS
                                    and now - began >= _SLOW_WINDOW and total - (done + len(buf)) >= _SLOW_MIN_REMAINING):
                                rate = _window_rate(samples, _SLOW_WINDOW)
                                if rate is not None:
                                    peak = max(peak, rate)
                                    if rate < max(_SLOW_FLOOR_BPS, _SLOW_FRACTION * peak):
                                        logger.info("[download] job=%s slow (%.1f MB/s, best so far %.1f) on server %s; reconnecting (%d/%d)",
                                                    ctx.job_id, rate / 1e6, peak / 1e6, sid, reconnects + 1, _SLOW_MAX_RECONNECTS)
                                        slow = True
                                        break
                            if len(buf) >= _WRITE_CHUNK:
                                await asyncio.to_thread(fh.write, bytes(buf))
                                done += len(buf)
                                buf.clear()
                                ctx.progress(done, shown)
                            ctx.check()
                        if buf:
                            await asyncio.to_thread(fh.write, bytes(buf))
                            done += len(buf)
                    ctx.progress(done, shown, force=True)
                    if slow:
                        reconnects += 1
                        attempt -= 1  # dropping a slow connection on purpose is not a failed attempt
                        await asyncio.sleep(1)
                        continue
                    if total and done != total:
                        raise httpx.ReadError("connection closed before the whole file arrived")
                    if done < 1024:
                        raise DownloadError("The provider sent an empty file.")
                    return done
        except (httpx.TransportError, httpx.HTTPError) as exc:
            if attempt == _MAX_ATTEMPTS:
                raise DownloadError(f"Connection problem: {_safe(exc) or type(exc).__name__}")
            logger.warning("[download] job=%s transfer interrupted (%s); retry %d/%d", ctx.job_id, type(exc).__name__, attempt, _MAX_ATTEMPTS - 1)
            await asyncio.sleep(5 * attempt)
    raise DownloadError("The transfer kept restarting.")


async def _copy_local(src: str, part: Path, ctx: _Ctx) -> int:
    if not os.path.isfile(src):
        raise DownloadError("The source file isn't there any more.")
    total = os.path.getsize(src)
    free = _free_bytes(ctx.out_root)
    if free is not None and free - total < ctx.min_free:
        raise DownloadError("Not enough free space for this file with the free-space limit you set.")
    ctx.check()
    await asyncio.to_thread(shutil.copyfile, src, part)
    ctx.progress(total, total, force=True)
    return total


ENCODERS = ("software", "auto", "vaapi", "qsv", "nvenc")
_ENCODER_LABELS = {"software": "CPU", "vaapi": "VAAPI", "vaapi-gpu": "VAAPI, all on the GPU", "qsv": "Intel QSV", "nvenc": "NVIDIA NVENC"}
DEFAULT_RENDER_DEVICE = "/dev/dri/renderD128"


def detect_hardware() -> dict:
    """What GPU access this container actually has (nothing is shared into a container unless compose asks for it)."""
    render = sorted(str(p) for p in Path("/dev/dri").glob("renderD*")) if Path("/dev/dri").is_dir() else []
    return {"render_devices": render, "nvidia": any(os.path.exists(p) for p in ("/dev/nvidiactl", "/dev/nvidia0"))}


def resolve_encoder(settings: dict | None) -> tuple[str, str | None, str | None]:
    """(engine, device, why-not): the engine to try first. A GPU engine whose device isn't in the container
    degrades to the CPU with a reason instead of failing every download."""
    settings = settings or {}
    want = settings.get("encoder") or "software"
    hw = detect_hardware()
    device = (settings.get("hw_device") or "").strip() or (hw["render_devices"][0] if hw["render_devices"] else DEFAULT_RENDER_DEVICE)
    if want == "auto":
        want = "vaapi" if hw["render_devices"] else ("nvenc" if hw["nvidia"] else "software")
    if want == "software":
        return "software", None, None
    if want in ("vaapi", "qsv") and not os.path.exists(device):
        return "software", None, f"{device} isn't available in the container"
    if want == "nvenc" and not hw["nvidia"]:
        return "software", None, "no NVIDIA GPU is available in the container"
    return want, (device if want != "nvenc" else None), None


def _video_args(preset: str, engine: str, device: str | None, threads: int) -> tuple[list[str], list[str]]:
    """(input-side options, output-side options) for the preset on this engine. The CPU decodes and scales and the GPU
    only encodes. GPU decoding is deliberately off: with the scale on the CPU every decoded frame would be copied
    back from the GPU, which measured about 25x slower than CPU decoding on an Intel iGPU."""
    p = PRESETS[preset]
    hevc = p["codec"] == "libx265"
    scale = f"scale=-2:'min({p['height']},ih)'"
    crf = str(p["crf"])
    if engine == "vaapi-gpu":
        # nothing to scale, so the frames can stay on the GPU from decode to encode (no copy back, the CPU stays idle)
        return (["-hwaccel", "vaapi", "-hwaccel_device", str(device), "-hwaccel_output_format", "vaapi"],
                ["-c:v", "hevc_vaapi" if hevc else "h264_vaapi", "-qp", crf])
    if engine == "vaapi":
        pre = ["-init_hw_device", f"vaapi=hw:{device}", "-filter_hw_device", "hw"]
        return pre, ["-vf", f"{scale},format=nv12,hwupload", "-c:v", "hevc_vaapi" if hevc else "h264_vaapi", "-qp", crf]
    if engine == "qsv":
        pre = ["-init_hw_device", f"vaapi=va:{device}", "-init_hw_device", "qsv=hw@va", "-filter_hw_device", "hw"]
        return pre, ["-vf", f"{scale},format=nv12,hwupload=extra_hw_frames=64", "-c:v", "hevc_qsv" if hevc else "h264_qsv",
                     "-global_quality", crf, "-preset", "medium"]
    if engine == "nvenc":
        return [], [
            "-vf", f"{scale},format=yuv420p", "-c:v", "hevc_nvenc" if hevc else "h264_nvenc",
            "-preset", "p4", "-rc", "vbr", "-cq", crf, "-b:v", "0"]
    post = ["-vf", scale, "-pix_fmt", "yuv420p", "-c:v", p["codec"], "-preset", "fast", "-crf", crf]
    if hevc:
        post += ["-x265-params", "log-level=error"]
    pre: list[str] = []
    if threads:     # the decoder and the filters spawn their own threads, so cap those too, not just the encoder
        pre = ["-threads", str(threads), "-filter_threads", str(threads)]
        post += ["-threads", str(threads)]
    return pre, post


def build_ffmpeg_args(src: str, dst: Path, preset: str, user_agent: str | None, is_http: bool,
                      plan: TrackPlan | None = None, copy: bool = False,
                      engine: str = "software", device: str | None = None, threads: int = 0) -> list[str]:
    """ffmpeg command. copy=True keeps every chosen stream as is (language trimming of an original);
    otherwise the video is re-encoded to the preset and the audio to AAC. Without a plan (the file could not be
    probed) all audio is kept and subtitles are dropped."""
    args = ["ffmpeg", "-hide_banner", "-nostdin", "-nostats", "-loglevel", "info", "-y", "-progress", "pipe:1"]
    if is_http:
        args += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "10"]
        args += ["-user_agent", user_agent or xc_server._UPSTREAM_HEADERS.get("User-Agent", "VOD-Manager")]
    hw_in, video_out = ([], []) if copy else _video_args(preset, engine, device, threads)
    args += hw_in
    args += ["-fflags", "+genpts+discardcorrupt", "-i", src]
    if plan is None:
        args += ["-map", "0:v:0", "-map", "0:a?", "-sn", "-dn"]
    else:
        if plan.video is not None:
            args += ["-map", f"0:{plan.video}"]
        for idx in plan.audio:
            args += ["-map", f"0:{idx}"]
        for idx, _codec in plan.subs:
            args += ["-map", f"0:{idx}"]
    if copy:
        args += ["-c", "copy"]
    else:
        args += video_out
        args += ["-c:a", "aac", "-b:a", "160k"]
    if plan is not None:
        for k, (_idx, codec) in enumerate(plan.subs):
            args += [f"-c:s:{k}", "srt" if codec in _TEXT_SUB_CONVERT else "copy"]
        if plan.audio:
            args += ["-disposition:a:0", "default"]
    args += ["-f", "matroska", str(dst)]
    if shutil.which("nice"):
        args = ["nice", "-n", "10", *args]
    return args


_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")


def _read_fraction(pid: int, src: str, total: int) -> float | None:
    """How far ffmpeg has read into its input file (Linux /proc). Used for the progress bar when the file carries no
    duration, which is common for provider MKVs."""
    try:
        want = os.path.realpath(src)
        for fd in Path(f"/proc/{pid}/fd").iterdir():
            try:
                if os.path.realpath(os.readlink(fd)) != want:
                    continue
                for line in Path(f"/proc/{pid}/fdinfo/{fd.name}").read_text().splitlines():
                    if line.startswith("pos:"):
                        return min(1.0, int(line.split()[1]) / total)
            except (OSError, ValueError):
                continue
    except OSError:
        pass
    return None


async def _probe_duration(path: str) -> float | None:
    if not shutil.which("ffprobe") or path.startswith(("http://", "https://")):
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), 60)
        return float(out.decode().strip()) or None
    except (asyncio.TimeoutError, ValueError, OSError):
        return None


async def _run_ffmpeg(src: str, part: Path, preset: str, user_agent: str | None, known_duration: int | None, ctx: _Ctx,
                      plan: TrackPlan | None = None, copy: bool = False,
                      engine: str = "software", device: str | None = None, threads: int = 0) -> int:
    is_http = src.startswith(("http://", "https://"))
    args = build_ffmpeg_args(src, part, preset, user_agent, is_http, plan, copy, engine, device, threads)
    in_total = os.path.getsize(src) if not is_http and os.path.isfile(src) else 0
    base_note = ctx.note
    proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    state = {"duration": float(known_duration) if known_duration else 0.0, "out_us": 0}
    tail: collections.deque = collections.deque(maxlen=6)

    async def pump_out():
        async for raw in proc.stdout:
            line = raw.decode(errors="replace").strip()
            if line.startswith(("out_time_us=", "out_time_ms=")):
                try:
                    state["out_us"] = max(state["out_us"], int(line.split("=", 1)[1]))
                except ValueError:
                    pass

    async def pump_err():
        async for raw in proc.stderr:
            line = _safe(raw.decode(errors="replace").rstrip())
            if not line:
                continue
            m = _DURATION_RE.search(line)
            if m and not state["duration"]:
                h, mi, sec = m.groups()
                state["duration"] = int(h) * 3600 + int(mi) * 60 + float(sec)
            tail.append(line)

    pumps = [asyncio.create_task(pump_out()), asyncio.create_task(pump_err())]
    waiter = asyncio.create_task(proc.wait())
    try:
        while True:
            done, _ = await asyncio.wait({waiter}, timeout=1.0)
            size = part.stat().st_size if part.exists() else 0
            if state["duration"]:
                frac = state["out_us"] / 1e6 / state["duration"]
            else:
                frac = (in_total and _read_fraction(proc.pid, src, in_total)) or 0.0
                if not frac and base_note:
                    ctx.note = f"{base_note} ({size / 1e6:,.0f} MB written so far)"
            ctx.progress(size, None, fraction=min(frac, 0.99))
            if done:
                break
            ctx.check()
    except BaseException:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        raise
    finally:
        await asyncio.gather(*pumps, return_exceptions=True)
    if proc.returncode != 0:
        msg = " | ".join(list(tail)[-3:]) or f"ffmpeg exited with code {proc.returncode}"
        raise DownloadError(f"Re-encode failed: {msg}")
    size = part.stat().st_size if part.exists() else 0
    if size < 1024:
        raise DownloadError("Re-encode produced an empty file.")
    ctx.progress(size, size, fraction=1.0, force=True)
    return size


async def test_encoder(engine: str, device: str = "") -> dict:
    """One-second test encode on the chosen engine, with the same options real downloads use."""
    if not ffmpeg_available():
        return {"ok": False, "engine": engine, "message": "ffmpeg isn't installed in this container."}
    eng, dev, why = resolve_encoder({"encoder": engine, "hw_device": device})
    if why:
        return {"ok": False, "engine": eng, "message": why[0].upper() + why[1:] + ". Share the GPU with the container (see the User Guide)."}
    # Same command shape as a real job: a 1080p source scaled down, 4 seconds, timed so a slow setup shows up.
    pre, post = _video_args("h264_720", eng, dev, 0)
    args = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y", *pre,
            "-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=30", "-t", "4", *post, "-an", "-f", "null", "-"]
    started = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        _, err = await asyncio.wait_for(proc.communicate(), 45)
    except asyncio.TimeoutError:
        proc.kill()
        return {"ok": False, "engine": eng, "message": "The test encode timed out."}
    if proc.returncode != 0:
        tail = " | ".join(_safe(err.decode(errors="replace")).strip().splitlines()[-3:])
        return {"ok": False, "engine": eng, "message": f"{_ENCODER_LABELS.get(eng, eng)} encode failed: {tail or 'ffmpeg error'}"}
    speed = 4 / max(time.monotonic() - started, 0.01)
    msg = f"{_ENCODER_LABELS.get(eng, eng)} works" + (f" on {dev}" if dev else "") + f" (about {speed:.1f}x real time on a 1080p test)."
    if speed < 0.5:
        msg += " That is slow; the CPU may be a better choice."
    return {"ok": True, "engine": eng, "device": dev, "speed": round(speed, 2), "message": msg}


# ── one job ─────────────────────────────────────────────────────────────────

def _is_remote_library(provider: dict) -> bool:
    return provider.get("provider_type") == "library" and xc_server.rclone_client.is_remote_backend(provider)


async def _fetch(job: dict, source: dict, dest: Path, ctx: _Ctx, settings: dict) -> tuple[int, str | None]:
    """Gets the ORIGINAL file for one source into dest while holding a provider connection (if it has one).
    Returns (size, local_path): local_path is set instead of copying when the source already is a file on
    disk and the caller only needs to read it (re-encode)."""
    provider = vod_db.get_provider(source["provider_id"])
    if not provider:
        raise DownloadError("The provider no longer exists.")
    ptype = provider.get("provider_type")
    local = ptype in ("dispatcharr_dvr", "library") and not _is_remote_library(provider)
    kind = "movie" if job["kind"] == "movie" else "series"
    user_agent = provider.get("custom_user_agent")

    if local:
        src = source.get("local_file_path") or ""
        if not os.path.isfile(src):
            raise DownloadError("The source file isn't there any more.")
        if job["preset"] != "original":
            return os.path.getsize(src), src
        return await _copy_local(src, dest, ctx), None

    pid = provider["id"]
    policy = provider_policy(provider, settings)
    now = time.time()
    if _cooldown_until.get(pid, 0) > now:
        wait = int(_cooldown_until[pid] - now)
        raise _AtCapacity(provider["name"], f"cooling down ({wait}s)", wait=wait)
    if _provider_active.get(pid, 0) >= policy["max_concurrent"]:
        raise _AtCapacity(provider["name"], "busy with another download")
    reservation = None
    if not _is_remote_library(provider):
        # Same connection gate playback uses: a download only starts when a connection is truly free, and
        # while it runs a viewer can't take that connection (see xc_server._try_reserve_capacity).
        reservation = await xc_server._select_upstream_credentials(provider, is_download=True)
        if reservation is None:
            streaming = xc_server._active_vod_streams.get(pid, 0) > 0
            raise _AtCapacity(provider["name"], "in use by playback" if streaming else "at its connection limit")
    _provider_active[pid] = _provider_active.get(pid, 0) + 1
    _job_provider[job["id"]] = pid
    ctx.rate_bps = int(policy["speed_mbps"] * 125_000)
    ctx.reconnect_slow = bool(settings.get("reconnect_slow", True))
    try:
        xc_server._active_sessions[ctx.session_id] = {
            "conn_id": ctx.session_id, "kind": kind, "title": f"{job['title']} (download)", "provider_name": provider["name"],
            "provider_type": ptype or "xc", "started_at": time.time(), "bytes_sent": 0, "total_bytes": 0, "duration_secs": None,
            "range_start_byte": 0, "plex_reported": False, "emby_reported": False,
        }
        vod_db.update_download_job(job["id"], ("running",), provider_name=provider["name"])
        url = await xc_server._build_upstream_url(kind, provider, source, reservation)
        try:
            size = await _download_http(url, dest, user_agent, ctx, provider["name"], source.get("file_size_bytes") or None)
        except _Pushback as pb:
            n = _pushbacks.get(pid, 0) + 1
            _pushbacks[pid] = n
            _cooldown_until[pid] = time.time() + min(_PUSHBACK_MAX_SECONDS, _PUSHBACK_BASE_SECONDS * 2 ** (n - 1))
            pb.count = n
            raise
        _pushbacks.pop(pid, None)
        return size, None
    finally:
        _job_provider.pop(job["id"], None)
        _provider_active[pid] = max(0, _provider_active.get(pid, 1) - 1)
        if policy["pause_seconds"]:
            _cooldown_until[pid] = max(_cooldown_until.get(pid, 0), time.time() + policy["pause_seconds"])
        xc_server._active_sessions.pop(ctx.session_id, None)
        xc_server._kill_requested.discard(ctx.session_id)
        if reservation is not None:
            xc_server._release_capacity(provider, reservation["sub_account_id"], is_download=True)


async def _transcode(src: str, out_part: Path, preset: str, duration: int | None, ctx: _Ctx,
                     plan: TrackPlan | None, copy: bool, settings: dict | None = None) -> int:
    """Runs ffmpeg for a re-encode or a language trim. An odd subtitle track that ffmpeg can't carry over must not
    sink the whole download, so a failure that involved subtitles is retried once without them. A GPU engine that
    fails (missing driver, unsupported source) falls back to the CPU instead of failing the download."""
    threads = int((settings or {}).get("encode_threads") or 0)
    engine, device, why = ("software", None, None) if copy else resolve_encoder(settings)
    base = ctx.note
    cpu_note = base
    if why:
        logger.warning("[download] job=%s: GPU encoding unavailable (%s); using the CPU", ctx.job_id, why)
        cpu_note = f"{base} (CPU: {why})"
    chain = [(engine, device)] + ([("software", None)] if engine != "software" else [])
    if engine == "vaapi" and not copy and _can_stay_on_gpu(preset, plan):
        chain.insert(0, ("vaapi-gpu", device))    # a decoder or format the GPU can't take falls through to the next step

    async def run(p: TrackPlan | None, eng: str, dev: str | None) -> int:
        if p is None and not copy:
            return await _run_ffmpeg(src, out_part, preset, None, duration, ctx, engine=eng, device=dev, threads=threads)
        return await _run_ffmpeg(src, out_part, preset, None, duration, ctx, plan=p, copy=copy, engine=eng, device=dev, threads=threads)

    async def with_subs_retry(eng: str, dev: str | None) -> int:
        try:
            return await run(plan, eng, dev)
        except DownloadError:
            if plan is not None and plan.subs:
                logger.warning("[download] job=%s: ffmpeg failed with subtitles, retrying without them", ctx.job_id)
                out_part.unlink(missing_ok=True)
                return await run(plan.without_subs(), eng, dev)
            raise

    for i, (eng, dev) in enumerate(chain):
        ctx.phase(ctx.lo, ctx.hi, (cpu_note if not i else f"{base} (the GPU encoder failed; using the CPU)") if eng == "software"
                  else f"{base} ({_ENCODER_LABELS[eng]})")
        try:
            return await with_subs_retry(eng, dev)
        except DownloadError as exc:
            if i == len(chain) - 1:
                raise
            logger.warning("[download] job=%s: %s encoding failed (%s); retrying on the CPU", ctx.job_id, eng, exc)
            out_part.unlink(missing_ok=True)


def _can_stay_on_gpu(preset: str, plan: TrackPlan | None) -> bool:
    """True when the source is already no taller than the preset, so nothing needs scaling on the CPU."""
    height = plan.src_height if plan is not None else None
    return bool(height) and height <= PRESETS[preset]["height"]


def _keeps_original(settings: dict, plan: TrackPlan | None, langs: list[str], src_size: int, out_size: int) -> bool:
    """A re-encode that doesn't shrink the file enough is not worth keeping. Not applied when tracks were dropped
    (the original still has them) or the language list couldn't be applied (no probe)."""
    pct = int(settings.get("min_saving_percent") or 0)
    if not pct or not src_size or (plan is not None and plan.changed) or (plan is None and langs):
        return False
    return out_size > src_size * (100 - pct) / 100


def _same_device(a: Path, b: Path) -> bool:
    try:
        return os.stat(a).st_dev == os.stat(b).st_dev
    except OSError:
        return False


async def _finalize(src: Path, final: Path, ctx: _Ctx) -> int:
    """Moves a finished, verified file from the processing folder into the library. On the same filesystem
    this is an instant rename; across filesystems it is copied under a hidden temporary name first, so the
    library never shows a partial file under the real name."""
    await asyncio.to_thread(final.parent.mkdir, parents=True, exist_ok=True)
    size = src.stat().st_size
    if _same_device(src, final.parent):
        await asyncio.to_thread(os.replace, src, final)
        return size
    free = _free_bytes(final.parent)
    if free is not None and free - size < ctx.min_free:
        raise DownloadError("Not enough free space in the library folder to move this file in with the free-space limit you set.")
    tmp = final.with_name("." + final.name + ".part")
    try:
        await asyncio.to_thread(shutil.copyfile, src, tmp)
        await asyncio.to_thread(os.replace, tmp, final)
    finally:
        tmp.unlink(missing_ok=True)
    src.unlink(missing_ok=True)
    return size


async def run_job(job_id: int) -> None:
    job = vod_db.get_download_job(job_id)
    if not job or job["status"] != "queued":
        return
    settings = config.get_download_settings()
    problem = validate_settings(settings)
    if problem or not settings["enabled"]:
        return
    _cancel_ids.discard(job_id)  # a stale cancel from before a retry must not kill the fresh run
    _preempt_ids.discard(job_id)
    vod_db.update_download_job(job_id, ("queued",), status="running", started_at=vod_db._now(), error=None, note=None)
    out_root = roots_for(settings)["movie" if job["kind"] == "movie" else "episode"]
    work = work_root_for(settings, out_root) / f"job-{job_id}"
    ctx = _Ctx(job_id, work, settings["min_free_gb"] * 10 ** 9, f"download-{job_id}")
    preset = job["preset"] if job["preset"] in PRESETS else "original"
    temps: list[Path] = []          # our own half-written files, removed if the job doesn't finish
    try:
        rel_dir, stem, duration = target_for(job)
        folder = out_root / rel_dir
        found = _existing_file(folder, stem)
        if found:
            vod_db.update_download_job(job_id, ("running",), status="done", progress=1.0, final_path=str(found.relative_to(out_root)),
                                       final_size=found.stat().st_size, note="Already in the library", finished_at=vod_db._now())
            vod_db.mark_import_waiting(job_id)  # hand the file to Radarr/Sonarr
            shutil.rmtree(work, ignore_errors=True)
            return
        sources = await asyncio.to_thread(_ordered_sources, job)
        if not sources:
            raise DownloadError("No playable source: the provider is inactive or the title was removed.")
        free = _free_bytes(work)
        if free is not None and settings["min_free_gb"] and free < ctx.min_free:
            raise DownloadError("Free space on the processing folder is already below the limit you set.")
        await asyncio.to_thread(work.mkdir, parents=True, exist_ok=True)

        errors: list[str] = []
        waiting_on: _AtCapacity | None = None
        pushed: _Pushback | None = None
        for source in sources:
            ctx.check()
            src_ext = _extension("original", source)
            part = work / f"{stem}.{src_ext}.part"       # original, still arriving (resumable)
            kept = work / f"{stem}.{src_ext}.src"        # original, fully fetched, waiting to be re-encoded
            for stale in list(work.glob(f"{glob_escape(stem)}.*.part")) + list(work.glob(f"{glob_escape(stem)}.*.src")):
                if stale not in (part, kept):
                    stale.unlink(missing_ok=True)
            source_name = source.get("provider_name") or "provider"
            try:
                local_input: str | None = None
                langs = languages_for(job, settings)
                keep_subs = bool(settings.get("keep_subtitles", True))
                will_process = preset != "original" or bool(langs)   # a re-encode, or trimming an original to some languages
                if not kept.exists():
                    ctx.phase(0.0, 0.5 if will_process else 1.0, "Downloading" if will_process else None)
                    temps.append(part)
                    size, local_input = await _fetch(job, source, part, ctx, settings)
                    if local_input is None:
                        await _validate_media(part)
                else:
                    size = kept.stat().st_size  # fetched before a restart: skip straight to processing
                held = kept if kept.exists() else part                 # our own copy of the original, if there is one
                plan = None
                if will_process and ffmpeg_available():
                    streams = await _probe_streams(local_input or held)
                    plan = plan_tracks(streams, langs, keep_subs) if streams else None
                trim_only = preset == "original"
                if trim_only and (plan is None or not plan.changed):
                    # nothing to remove (or can't tell): the original goes in untouched
                    final = folder / f"{stem}.{src_ext}"
                    await _finalize(held, final, ctx)
                    temps.clear()
                else:
                    if local_input is None and part.exists():
                        await asyncio.to_thread(os.replace, part, kept)
                    encode_in = local_input or str(kept)
                    out_part = work / f"{stem}.mkv.part"
                    temps = [out_part] + ([kept] if local_input is None else [])
                    out_part.unlink(missing_ok=True)
                    ctx.phase(0.5 if local_input is None else 0.0, 1.0, "Removing other languages" if trim_only else "Re-encoding")
                    if not duration:
                        duration = await _probe_duration(encode_in)     # provider files often carry no runtime in the title
                    size = await _transcode(encode_in, out_part, preset, duration, ctx, plan, copy=trim_only, settings=settings)
                    await _validate_media(out_part)      # the new file is checked too, before it reaches the library
                    out_size = out_part.stat().st_size
                    if local_input is None and not trim_only and _keeps_original(settings, plan, langs, kept.stat().st_size, out_size):
                        logger.info("[download] job=%s: re-encode %d -> %d bytes saves less than %d%%; keeping the original",
                                    job_id, kept.stat().st_size, out_size, settings["min_saving_percent"])
                        out_part.unlink(missing_ok=True)
                        final = folder / f"{stem}.{src_ext}"
                        size = await _finalize(kept, final, ctx)
                    else:
                        final = folder / f"{stem}.mkv"
                        size = await _finalize(out_part, final, ctx)
                    if local_input is None:
                        kept.unlink(missing_ok=True)   # only ever our own temp copy, never a library file
                    temps.clear()
            except _AtCapacity as cap:
                waiting_on = waiting_on or cap
                continue
            except _Pushback as pb:
                pushed = pushed or pb
                continue
            except DownloadError as exc:
                errors.append(f"{source_name}: {exc}")
                if "free space" in str(exc).lower():
                    raise
                kept.unlink(missing_ok=True)
                continue
            vod_db.update_download_job(job_id, ("running",), status="done", progress=1.0, bytes_done=size, total_bytes=size,
                                       final_path=str(final.relative_to(out_root)), final_size=size, note=None, error=None,
                                       finished_at=vod_db._now())
            vod_db.mark_import_waiting(job_id)  # hand the file to Radarr/Sonarr
            shutil.rmtree(work, ignore_errors=True)
            logger.info("[download] job=%s done: %s (%d bytes, preset=%s)", job_id, final.relative_to(out_root), size, preset)
            return
        if pushed and not errors:
            n = getattr(pushed, "count", 1)
            if n >= _PUSHBACK_GIVE_UP:
                raise DownloadError(f"{pushed.provider_name} keeps refusing downloads (HTTP {pushed.status}). Check the provider's rules, "
                                    f"lower its speed or concurrency in Providers -> Downloads, then retry.")
            wait = min(_PUSHBACK_MAX_SECONDS, _PUSHBACK_BASE_SECONDS * 2 ** (n - 1))
            vod_db.update_download_job(job_id, ("running",), status="queued", next_try_at=time.time() + wait,
                                       note=f"{pushed.provider_name} said HTTP {pushed.status} (slow down). Trying again in {wait // 60} min")
            logger.warning("[download] job=%s provider %s pushed back (HTTP %s), attempt %d; waiting %ds", job_id, pushed.provider_name, pushed.status, n, wait)
            return
        if waiting_on and not errors:
            wait = max(_CAPACITY_WAIT_SECONDS, getattr(waiting_on, "wait", 0))
            vod_db.update_download_job(job_id, ("running",), status="queued", next_try_at=time.time() + wait,
                                       note=f"Waiting: {waiting_on.provider_name} is {waiting_on.why}")
            return
        raise DownloadError("; ".join(errors) if errors else "No source could be downloaded.")
    except _Preempted:
        # keep the partial file: it resumes where it stopped once the connection is free again
        _preempt_ids.discard(job_id)
        vod_db.update_download_job(job_id, ("running",), status="queued", next_try_at=time.time() + 30,
                                   note="Paused for a viewer. Resumes when the connection is free")
        logger.info("[download] job=%s paused for a viewer", job_id)
        temps.clear()
    except _Cancelled:
        logger.info("[download] job=%s cancelled", job_id)
        shutil.rmtree(work, ignore_errors=True)
        temps.clear()
    except DownloadError as exc:
        vod_db.update_download_job(job_id, ("running",), status="failed", error=_safe(exc), note=None, finished_at=vod_db._now())
        logger.warning("[download] job=%s failed: %s", job_id, _safe(exc))
    except Exception as exc:  # never let one job take the worker down
        vod_db.update_download_job(job_id, ("running",), status="failed", error=f"{type(exc).__name__}: {_safe(exc)}", note=None,
                                   finished_at=vod_db._now())
        logger.exception("[download] job=%s crashed", job_id)
    finally:
        _cancel_ids.discard(job_id)
        _preempt_ids.discard(job_id)
        # A plain download's partial stays so a retry can resume it; a half-encoded file can't be resumed.
        for t in temps:
            if t.suffix == ".part" and t.name.endswith(".mkv.part"):
                t.unlink(missing_ok=True)


def glob_escape(text: str) -> str:
    return re.sub(r"([\[\]*?])", r"[\1]", text)


# ── worker ──────────────────────────────────────────────────────────────────

async def download_loop() -> None:
    """Background worker: runs up to max_concurrent queued jobs. Does nothing while downloads are off."""
    await asyncio.sleep(20)
    resumed = await asyncio.to_thread(vod_db.requeue_interrupted_downloads)
    if resumed:
        logger.info("[download] %d interrupted download(s) put back in the queue", resumed)
    while True:
        try:
            for jid, task in list(_running.items()):
                if task.done():
                    _running.pop(jid, None)
            settings = config.get_download_settings()
            if settings["enabled"] and validate_settings(settings) is None and in_window(settings):
                slots = settings["max_concurrent"] - len(_running)
                if slots > 0:
                    for job in await asyncio.to_thread(vod_db.next_download_jobs, slots, time.time(), set(_running)):
                        _running[job["id"]] = asyncio.create_task(run_job(job["id"]))
        except Exception as exc:
            logger.warning("[download] worker loop error: %s", exc)
        await asyncio.sleep(3)
