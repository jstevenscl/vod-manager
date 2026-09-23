"""Filename/path parsing for library sources (local/SMB/NFS/rclone).

Pure logic, no I/O: takes a path relative to the source root and returns what
can be inferred about it (title, year, season/episode, explicit provider IDs).
Matching against TMDB lives elsewhere -- this module only decides what to
search for, and whether the path already names its own TMDB/IMDb id.

Precedence for movie-vs-episode and title: an explicit SxxExx (or NxNN) in
the filename marks an episode, and the show title then comes from the folder
above any "Season N" folder; otherwise it is a movie titled from the filename,
falling back to the parent folder when the filename is only junk.
"""

import os
import re
from dataclasses import dataclass, field

VIDEO_EXTENSIONS = frozenset({
    ".mkv", ".mp4", ".avi", ".m4v", ".mov", ".wmv", ".ts", ".m2ts", ".mpg", ".mpeg", ".webm", ".flv",
})

# {tmdb-123} / [tmdbid=123] / (tmdb 123), Plex/Jellyfin style.
_TMDB_ID_RE = re.compile(r"[\[{(]\s*tmdb(?:id)?\s*[-=: ]\s*(\d+)\s*[\]})]", re.I)
_IMDB_ID_RE = re.compile(r"[\[{(]?\s*(?:imdb(?:id)?\s*[-=: ]\s*)?(tt\d{6,9})\s*[\]})]?", re.I)

_SXXEXX_RE = re.compile(r"(?<![a-z0-9])s(\d{1,2})[ ._-]?e(\d{1,3})(?:[ ._-]?(?:e|-e?)(\d{1,3}))?(?![a-z0-9])", re.I)
_NXNN_RE = re.compile(r"(?<![a-z0-9])(\d{1,2})x(\d{2,3})(?![a-z0-9])", re.I)
_SEASON_DIR_RE = re.compile(r"^(?:season|series|s)[ ._-]?(\d{1,2})$", re.I)
_SPECIALS_DIR_RE = re.compile(r"^specials?$", re.I)

# A plausible release year: 1900-2099, not part of a longer number.
_YEAR_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")

# Everything from the first of these tokens onward is release junk, not title.
_JUNK_TOKENS = (
    r"2160p|1080[pi]|720p|576p|480p|4k|uhd|hdr10\+?|hdr|dv|dolby[ .]?vision|"
    r"bluray|blu-ray|bdrip|brrip|bdremux|remux|web-?dl|web-?rip|webrip|hdtv|dvdrip|dvdscr|"
    r"hdrip|cam|telesync|x26[45]|h[ .]?26[45]|hevc|avc|xvid|divx|av1|"
    r"aac|ac3|eac3|dts(?:-?hd)?|truehd|atmos|ddp?[0-9][ .]?[0-9]|flac|"
    r"extended|unrated|directors[ .]cut|remastered|proper|repack|internal|"
    r"multi|dual|subbed|dubbed|10bit|8bit"
)
_JUNK_RE = re.compile(rf"(?<![a-z0-9])(?:{_JUNK_TOKENS})(?![a-z0-9])", re.I)
# Filenames that say nothing about the content; the parent folder is the title.
_GENERIC_NAMES = frozenset({"movie", "film", "video", "main", "title", "feature", "sample"})
_BRACKET_RE = re.compile(r"\[[^\]]*\]|\{[^}]*\}")
_TRAILING_GROUP_RE = re.compile(r"-[A-Za-z0-9]{2,12}$")


@dataclass
class ParsedPath:
    kind: str                       # "movie" | "episode"
    title: str                      # cleaned title to search TMDB with
    year: int | None = None
    season: int | None = None
    episode: int | None = None
    episode_end: int | None = None  # multi-episode files (S01E01-E02)
    tmdb_id: str | None = None
    imdb_id: str | None = None
    show_key: str | None = None     # folder path identifying one series; resolve TMDB once per key
    warnings: list[str] = field(default_factory=list)


def is_video_file(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in VIDEO_EXTENSIONS


def _find_ids(*segments: str) -> tuple[str | None, str | None]:
    tmdb = imdb = None
    for seg in segments:
        if tmdb is None and (m := _TMDB_ID_RE.search(seg)):
            tmdb = m.group(1)
        if imdb is None and (m := _IMDB_ID_RE.search(seg)):
            imdb = m.group(1).lower()
    return tmdb, imdb


def _clean_title(raw: str) -> tuple[str, int | None]:
    """Turns a release-style name into (title, year). Cuts at the first junk
    token or year found AFTER at least one title word, so a title that is
    itself a year ("1917", "2012") survives."""
    s = _BRACKET_RE.sub(" ", raw)
    s = re.sub(r"[._]+", " ", s)
    s = _TMDB_ID_RE.sub(" ", s)
    s = re.sub(r"\(\s*(tt\d{6,9})\s*\)", " ", s)

    cut = len(s)
    year = None

    junk = _JUNK_RE.search(s)
    if junk:
        cut = junk.start()

    # Year: the LAST plausible year before the junk cut that has title text
    # before it -- so "Blade Runner 2049 2017" reads 2049 as part of the
    # title and 2017 as the release year, and "1917 2019" keeps "1917".
    for m in reversed(list(_YEAR_RE.finditer(s[:cut]))):
        if s[:m.start()].strip(" ([-"):
            year = int(m.group(1))
            cut = m.start()
            break

    title = s[:cut]
    title = re.sub(r"[\(\[\-\s]+$", "", title)          # dangling "(" or "-"
    title = _TRAILING_GROUP_RE.sub("", title) if not junk and not year else title
    title = re.sub(r"\s+", " ", title).strip(" -.")
    if title.lower() in _GENERIC_NAMES:
        title = ""
    return title, year


def _season_from_dir(name: str) -> int | None:
    if _SPECIALS_DIR_RE.match(name):
        return 0
    m = _SEASON_DIR_RE.match(name)
    return int(m.group(1)) if m else None


def parse_path(rel_path: str) -> ParsedPath:
    """rel_path is relative to the library root, either slash style."""
    parts = [p for p in rel_path.replace("\\", "/").split("/") if p]
    if not parts:
        return ParsedPath(kind="movie", title="")
    filename = os.path.splitext(parts[-1])[0]
    folders = parts[:-1]

    tmdb, imdb = _find_ids(filename, *reversed(folders))

    ep = _SXXEXX_RE.search(filename)
    nx = None
    season = episode = episode_end = None
    if ep:
        season, episode = int(ep.group(1)), int(ep.group(2))
        episode_end = int(ep.group(3)) if ep.group(3) else None
    else:
        nx = _NXNN_RE.search(filename)
        if nx:
            season, episode = int(nx.group(1)), int(nx.group(2))

    if episode is not None:
        # Show folder = nearest ancestor that isn't a "Season N"/"Specials" dir.
        idx = len(folders) - 1
        while idx >= 0 and _season_from_dir(folders[idx]) is not None:
            if season is None:
                season = _season_from_dir(folders[idx])
            idx -= 1
        if idx >= 0:
            show_folder = folders[idx]
            title, year = _clean_title(show_folder)
            show_key = "/".join(folders[:idx + 1])
        else:
            # Flat layout: show title is whatever precedes SxxExx in the filename.
            marker = ep or nx
            title, year = _clean_title(filename[:marker.start()] if marker else filename)
            show_key = title.lower() or None
        parsed = ParsedPath("episode", title, year, season, episode, episode_end, tmdb, imdb, show_key)
        if not title:
            parsed.warnings.append("no show title found")
        return parsed

    title, year = _clean_title(filename)
    warnings: list[str] = []
    if not title and folders:
        # Filename was pure junk (e.g. "movie.mkv"/"1080p.mkv"): use the folder.
        title, year = _clean_title(folders[-1])
        warnings.append("title taken from parent folder")
    elif folders and year is None:
        # "Movie Name/movie.mkv"-style: the folder often carries the year.
        _, folder_year = _clean_title(folders[-1])
        year = folder_year
    if not title:
        warnings.append("no title found")
    return ParsedPath("movie", title, year, tmdb_id=tmdb, imdb_id=imdb, warnings=warnings)
