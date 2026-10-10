"""
Local TMDB title store -- a separate SQLite file (DATA_DIR/tmdb.sqlite) that
keeps every TMDB movie/TV detail payload this app has fetched, so repeat
lookups are answered locally instead of calling TMDB again.

Pure storage: no network calls here. tmdb_sync fetches (and reads through
this store first); tmdb_fill runs the background pre-fill and refresh.

One row per (media_type, tmdb_id) in `titles` with the commonly used fields
as columns plus the (trimmed, zlib-compressed) raw payload. Every known name
for a title (title, original title, alternative titles) goes in
`title_names`, indexed by FTS5 for name lookups. `export_ids` mirrors TMDB's
daily ID export and is the work list for the background pre-fill.
"""

import gzip
import json
import re
import sqlite3
import time
import unicodedata
import zlib
from contextlib import contextmanager
from typing import Iterator
from datetime import datetime, timezone
from pathlib import Path

from config import DATA_DIR

DB_PATH = DATA_DIR / "tmdb.sqlite"
MEDIA_TYPES = ("movie", "tv")
RAW_CAST_LIMIT = 20
_TOP_CAST = 10
_KEEP_CREW_JOBS = {"Director", "Creator", "Executive Producer"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS titles (
    media_type      TEXT NOT NULL,
    tmdb_id         INTEGER NOT NULL,
    title           TEXT,
    original_title  TEXT,
    year            INTEGER,
    release_date    TEXT,
    poster_path     TEXT,
    overview        TEXT,
    vote_average    REAL,
    popularity      REAL,
    content_rating  TEXT,
    top_cast        TEXT,
    genres          TEXT,
    raw             BLOB,
    fetched_at      REAL NOT NULL,
    changed_at      REAL,
    imdb_id         TEXT,
    PRIMARY KEY (media_type, tmdb_id)
);
CREATE TABLE IF NOT EXISTS title_names (
    id          INTEGER PRIMARY KEY,
    media_type  TEXT NOT NULL,
    tmdb_id     INTEGER NOT NULL,
    name        TEXT NOT NULL,
    norm        TEXT NOT NULL,
    kind        TEXT NOT NULL,
    country     TEXT
);
CREATE INDEX IF NOT EXISTS title_names_by_id ON title_names(media_type, tmdb_id);
CREATE VIRTUAL TABLE IF NOT EXISTS title_names_fts USING fts5(
    name, content='title_names', content_rowid='id', tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS title_names_ai AFTER INSERT ON title_names BEGIN
    INSERT INTO title_names_fts(rowid, name) VALUES (new.id, new.name);
END;
CREATE TRIGGER IF NOT EXISTS title_names_ad AFTER DELETE ON title_names BEGIN
    INSERT INTO title_names_fts(title_names_fts, rowid, name) VALUES ('delete', old.id, old.name);
END;
CREATE TABLE IF NOT EXISTS export_ids (
    media_type  TEXT NOT NULL,
    tmdb_id     INTEGER NOT NULL,
    name        TEXT,
    popularity  REAL,
    name_key    INTEGER,
    PRIMARY KEY (media_type, tmdb_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS export_ids_by_popularity ON export_ids(media_type, popularity DESC);
CREATE TABLE IF NOT EXISTS gone (
    media_type  TEXT NOT NULL,
    tmdb_id     INTEGER NOT NULL,
    PRIMARY KEY (media_type, tmdb_id)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS seasons (
    tmdb_id        INTEGER NOT NULL,
    season_number  INTEGER NOT NULL,
    episodes       BLOB NOT NULL,
    fetched_at     REAL NOT NULL,
    PRIMARY KEY (tmdb_id, season_number)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS meta (
    key    TEXT PRIMARY KEY,
    value  TEXT
);
"""
# After _upgrade, so they also apply to a store created before these columns existed.
_INDEXES = """
CREATE INDEX IF NOT EXISTS titles_by_imdb ON titles(imdb_id) WHERE imdb_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS export_ids_by_name ON export_ids(media_type, name_key);
"""

_initialized_path: Path | None = None


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False, timeout=30.0)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def _open() -> Iterator[sqlite3.Connection]:
    # sqlite3's own `with conn` only commits -- it never closes, so the
    # file stayed open and clear() couldn't delete it on Windows. 2026-10-05
    conn = _connect()
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init_db() -> None:
    global _initialized_path
    with _open() as conn:
        conn.executescript(_SCHEMA)
        _upgrade(conn)
        conn.executescript(_INDEXES)
    _initialized_path = DB_PATH


def _upgrade(conn: sqlite3.Connection) -> None:
    """Add columns introduced after a store was first created."""
    def columns(table: str) -> set[str]:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}

    if "imdb_id" not in columns("titles"):
        conn.execute("ALTER TABLE titles ADD COLUMN imdb_id TEXT")
        updates = []
        for row in conn.execute("SELECT media_type, tmdb_id, raw FROM titles WHERE raw IS NOT NULL").fetchall():
            imdb = _imdb_id(json.loads(zlib.decompress(row["raw"])))
            if imdb:
                updates.append((imdb, row["media_type"], row["tmdb_id"]))
        conn.executemany("UPDATE titles SET imdb_id=? WHERE media_type=? AND tmdb_id=?", updates)
    if "name_key" not in columns("export_ids"):
        conn.execute("ALTER TABLE export_ids ADD COLUMN name_key INTEGER")
        # existing rows have no name_key -- re-import the export on the next fill tick
        conn.execute("UPDATE meta SET value='0' WHERE key='exports_imported_at'")


def clear() -> None:
    """Delete the whole store to free its disk space (recreated empty on next use)."""
    global _initialized_path
    _initialized_path = None
    for path in DB_PATH.parent.glob(DB_PATH.name + "*"):
        path.unlink(missing_ok=True)


def _conn():
    if _initialized_path != DB_PATH:
        init_db()
    return _open()


def normalize(text: str | None) -> str:
    """Lowercase, accent-stripped, punctuation collapsed to single spaces."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).lower()
    return " ".join(re.sub(r"[^\w]+", " ", text).split())


def name_key(norm: str) -> int | None:
    """Compact index key for an exact normalized name. A crc32 collision only
    makes search_confident more cautious, never wrong."""
    return zlib.crc32(norm.encode()) if norm else None


def _imdb_id(data: dict) -> str | None:
    return data.get("imdb_id") or (data.get("external_ids") or {}).get("imdb_id") or None


def _year(date: str | None) -> int | None:
    return int(date[:4]) if date and len(date) >= 4 and date[:4].isdigit() else None


def _us_rating(media_type: str, data: dict) -> str | None:
    if media_type == "movie":
        for country in data.get("release_dates", {}).get("results", []):
            if country.get("iso_3166_1") == "US":
                for release in country.get("release_dates", []):
                    cert = (release.get("certification") or "").strip()
                    if cert:
                        return cert
        return None
    for country in data.get("content_ratings", {}).get("results", []):
        if country.get("iso_3166_1") == "US":
            return (country.get("rating") or "").strip() or None
    return None


def _trim(data: dict) -> dict:
    """Long-running shows carry hundreds of credits; keep what callers use."""
    data = dict(data)
    credits = data.get("credits")
    if isinstance(credits, dict):
        data["credits"] = {
            "cast": (credits.get("cast") or [])[:RAW_CAST_LIMIT],
            "crew": [c for c in credits.get("crew") or [] if c.get("job") in _KEEP_CREW_JOBS],
        }
    return data


def _alt_titles(media_type: str, data: dict) -> list[dict]:
    alt = data.get("alternative_titles") or {}
    return alt.get("titles" if media_type == "movie" else "results") or []


def upsert_payload(media_type: str, data: dict) -> None:
    """Store one TMDB detail payload (/movie/{id} or /tv/{id}, ideally with
    credits, release_dates/content_ratings and alternative_titles appended)."""
    tmdb_id = int(data["id"])
    is_movie = media_type == "movie"
    title = data.get("title" if is_movie else "name")
    original = data.get("original_title" if is_movie else "original_name")
    date = data.get("release_date" if is_movie else "first_air_date") or None
    cast = [c.get("name") for c in (data.get("credits") or {}).get("cast", [])[:_TOP_CAST] if c.get("name")]
    raw = zlib.compress(json.dumps(_trim(data), separators=(",", ":")).encode())
    names = [(title, "title", None), (original, "original", None)]
    names += [(a.get("title"), "alt", a.get("iso_3166_1")) for a in _alt_titles(media_type, data)]
    seen: set[str] = set()
    name_rows = []
    for name, kind, country in names:
        norm = normalize(name)
        if norm and norm not in seen:
            seen.add(norm)
            name_rows.append((media_type, tmdb_id, name, norm, kind, country))
    with _conn() as conn:
        conn.execute(
            """INSERT INTO titles (media_type, tmdb_id, title, original_title, year, release_date,
                   poster_path, overview, vote_average, popularity, content_rating, top_cast, genres,
                   raw, fetched_at, changed_at, imdb_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,?)
               ON CONFLICT(media_type, tmdb_id) DO UPDATE SET
                   title=excluded.title, original_title=excluded.original_title, year=excluded.year,
                   release_date=excluded.release_date, poster_path=excluded.poster_path,
                   overview=excluded.overview, vote_average=excluded.vote_average,
                   popularity=excluded.popularity, content_rating=excluded.content_rating,
                   top_cast=excluded.top_cast, genres=excluded.genres, raw=excluded.raw,
                   fetched_at=excluded.fetched_at, changed_at=NULL, imdb_id=excluded.imdb_id""",
            (
                media_type, tmdb_id, title, original, _year(date), date,
                data.get("poster_path") or None, data.get("overview") or None,
                data.get("vote_average") or None, data.get("popularity"),
                _us_rating(media_type, data), ", ".join(cast) or None,
                ", ".join(g["name"] for g in data.get("genres") or [] if g.get("name")) or None,
                raw, time.time(), _imdb_id(data),
            ),
        )
        conn.execute("DELETE FROM title_names WHERE media_type=? AND tmdb_id=?", (media_type, tmdb_id))
        conn.executemany(
            "INSERT INTO title_names (media_type, tmdb_id, name, norm, kind, country) VALUES (?,?,?,?,?,?)",
            name_rows,
        )
        conn.execute("DELETE FROM gone WHERE media_type=? AND tmdb_id=?", (media_type, tmdb_id))


def get_title(media_type: str, tmdb_id: int) -> dict | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM titles WHERE media_type=? AND tmdb_id=?", (media_type, int(tmdb_id))
        ).fetchone()
    if row is None:
        return None
    out = dict(row)
    out.pop("raw", None)
    return out


def get_payload(media_type: str, tmdb_id: int) -> dict | None:
    """The stored raw payload, or None when missing or flagged as changed on
    TMDB since it was fetched (the caller should refetch)."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT raw, fetched_at, changed_at FROM titles WHERE media_type=? AND tmdb_id=?",
            (media_type, int(tmdb_id)),
        ).fetchone()
    if row is None or row["raw"] is None or (row["changed_at"] and row["changed_at"] > row["fetched_at"]):
        return None
    return json.loads(zlib.decompress(row["raw"]))


def _fts_query(norm: str) -> str:
    return " ".join(f'"{token}"' for token in norm.split())


def search(media_type: str, query: str, year: int | None = None, limit: int = 10) -> list[dict]:
    """Name lookup over every stored name. Exact (normalized) name matches
    rank first, then a release-year match (+/-1), then TMDB popularity."""
    norm = normalize(query)
    if not norm:
        return []
    with _conn() as conn:
        rows = conn.execute(
            """SELECT t.media_type, t.tmdb_id, t.title, t.original_title, t.year, t.release_date,
                      t.poster_path, t.overview, t.vote_average, t.popularity, t.content_rating,
                      t.top_cast, t.genres, t.fetched_at, n.norm
               FROM title_names_fts f
               JOIN title_names n ON n.id = f.rowid
               JOIN titles t ON t.media_type = n.media_type AND t.tmdb_id = n.tmdb_id
               WHERE title_names_fts MATCH ? AND n.media_type = ?
               LIMIT 500""",
            (_fts_query(norm), media_type),
        ).fetchall()
    best: dict[int, tuple] = {}
    for row in rows:
        exact = row["norm"] == norm
        year_ok = year is not None and row["year"] is not None and abs(row["year"] - year) <= 1
        key = (exact, year_ok, row["popularity"] or 0.0)
        current = best.get(row["tmdb_id"])
        if current is None or key > current[0]:
            best[row["tmdb_id"]] = (key, row)
    ranked = sorted(best.values(), key=lambda item: item[0], reverse=True)[:limit]
    out = []
    for key, row in ranked:
        item = dict(row)
        item.pop("norm", None)
        item["exact"] = key[0]
        out.append(item)
    return out


def search_confident(media_type: str, query: str, year: int | None = None, limit: int = 10) -> list[dict] | None:
    """search(), or None when the local answer may be incomplete and TMDB
    should be asked instead. Confident only when a stored title matches the
    name exactly AND every title TMDB's ID export lists under that exact
    (original) name is stored or known gone -- so a same-named title the
    store doesn't have yet is never silently missed."""
    hits = search(media_type, query, year, limit)
    if not any(h["exact"] for h in hits):
        return None
    with _conn() as conn:
        listed, missing = conn.execute(
            """SELECT COUNT(*),
                      SUM(NOT EXISTS (SELECT 1 FROM titles t WHERE t.media_type=e.media_type AND t.tmdb_id=e.tmdb_id)
                          AND NOT EXISTS (SELECT 1 FROM gone g WHERE g.media_type=e.media_type AND g.tmdb_id=e.tmdb_id))
               FROM export_ids e WHERE e.media_type=? AND e.name_key=?""",
            (media_type, name_key(normalize(query))),
        ).fetchone()
    return hits if listed and not missing else None


def find_by_imdb(media_type: str, imdb_id: str) -> dict | None:
    with _conn() as conn:
        row = conn.execute(
            """SELECT media_type, tmdb_id, title, original_title, year, popularity FROM titles
               WHERE imdb_id=? AND media_type=? LIMIT 1""",
            (imdb_id, media_type),
        ).fetchone()
    return dict(row) if row else None


def get_season(tmdb_id: int, season_number: int, max_age: float | None = None) -> list[dict] | None:
    """Stored episode list for one TV season, or None when missing or older
    than max_age seconds. Rows are dropped when TMDB reports the show changed."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT episodes, fetched_at FROM seasons WHERE tmdb_id=? AND season_number=?",
            (int(tmdb_id), int(season_number)),
        ).fetchone()
    if row is None or (max_age is not None and time.time() - row["fetched_at"] > max_age):
        return None
    return json.loads(zlib.decompress(row["episodes"]))


def put_season(tmdb_id: int, season_number: int, episodes: list[dict]) -> None:
    raw = zlib.compress(json.dumps(episodes, separators=(",", ":")).encode())
    with _conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO seasons (tmdb_id, season_number, episodes, fetched_at) VALUES (?,?,?,?)",
            (int(tmdb_id), int(season_number), raw, time.time()),
        )


def mark_changed(media_type: str, tmdb_ids: list[int]) -> int:
    now = time.time()
    with _conn() as conn:
        if media_type == "tv":
            conn.executemany("DELETE FROM seasons WHERE tmdb_id=?", [(int(i),) for i in tmdb_ids])
        cur = conn.executemany(
            "UPDATE titles SET changed_at=? WHERE media_type=? AND tmdb_id=?",
            [(now, media_type, int(i)) for i in tmdb_ids],
        )
        return cur.rowcount


def mark_gone(media_type: str, tmdb_id: int) -> None:
    with _conn() as conn:
        conn.execute("INSERT OR IGNORE INTO gone (media_type, tmdb_id) VALUES (?,?)", (media_type, int(tmdb_id)))
        conn.execute("DELETE FROM titles WHERE media_type=? AND tmdb_id=?", (media_type, int(tmdb_id)))
        conn.execute("DELETE FROM title_names WHERE media_type=? AND tmdb_id=?", (media_type, int(tmdb_id)))
        if media_type == "tv":
            conn.execute("DELETE FROM seasons WHERE tmdb_id=?", (int(tmdb_id),))


def list_stale_ids(media_type: str, limit: int) -> list[int]:
    with _conn() as conn:
        rows = conn.execute(
            """SELECT tmdb_id FROM titles WHERE media_type=? AND changed_at > fetched_at
               ORDER BY popularity DESC LIMIT ?""",
            (media_type, limit),
        ).fetchall()
    return [r[0] for r in rows]


def list_fill_candidates(media_type: str, limit: int, top_n: int) -> list[int]:
    """Most popular export ids (within the top_n) not yet stored or known gone."""
    with _conn() as conn:
        rows = conn.execute(
            """SELECT e.tmdb_id FROM (
                   SELECT tmdb_id, popularity FROM export_ids WHERE media_type=?
                   ORDER BY popularity DESC LIMIT ?
               ) e
               WHERE NOT EXISTS (SELECT 1 FROM titles t WHERE t.media_type=? AND t.tmdb_id=e.tmdb_id)
                 AND NOT EXISTS (SELECT 1 FROM gone g WHERE g.media_type=? AND g.tmdb_id=e.tmdb_id)
               ORDER BY e.popularity DESC LIMIT ?""",
            (media_type, top_n, media_type, media_type, limit),
        ).fetchall()
    return [r[0] for r in rows]


def import_export_file(media_type: str, path: Path) -> int:
    """Replace export_ids for media_type from a TMDB daily export (.json.gz,
    one JSON object per line). Adult and video-only entries are skipped."""
    name_field = "original_title" if media_type == "movie" else "original_name"
    batch: list[tuple] = []
    count = 0
    with _conn() as conn:
        conn.execute("DELETE FROM export_ids WHERE media_type=?", (media_type,))
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                if item.get("adult") or item.get("video") or "id" not in item:
                    continue
                name = item.get(name_field)
                batch.append((media_type, int(item["id"]), name, item.get("popularity") or 0.0,
                              name_key(normalize(name))))
                if len(batch) >= 5000:
                    conn.executemany("INSERT OR REPLACE INTO export_ids VALUES (?,?,?,?,?)", batch)
                    count += len(batch)
                    batch.clear()
        if batch:
            conn.executemany("INSERT OR REPLACE INTO export_ids VALUES (?,?,?,?,?)", batch)
            count += len(batch)
    return count


def get_meta(key: str, default: str | None = None) -> str | None:
    with _conn() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set_meta(key: str, value) -> None:
    with _conn() as conn:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )


def _today_key(prefix: str = "requests") -> str:
    return f"{prefix}:" + datetime.now(timezone.utc).strftime("%Y-%m-%d")


def requests_today() -> int:
    return int(get_meta(_today_key(), "0") or 0)


def _add_counter(key: str, count: int) -> None:
    with _conn() as conn:
        conn.execute(
            """INSERT INTO meta (key, value) VALUES (?, ?)
               ON CONFLICT(key) DO UPDATE SET value = CAST(value AS INTEGER) + CAST(excluded.value AS INTEGER)""",
            (key, str(count)),
        )


def add_requests(count: int) -> None:
    _add_counter(_today_key(), count)


# Local-vs-TMDB hit rate for the TMDB Library page -- every
# app lookup (not the background fill) counts as answered locally or by TMDB.
def count_lookup(local: bool) -> None:
    _add_counter(_today_key("lookups_local" if local else "lookups_tmdb"), 1)


def lookups_today() -> dict:
    return {"local": int(get_meta(_today_key("lookups_local"), "0") or 0),
            "tmdb": int(get_meta(_today_key("lookups_tmdb"), "0") or 0)}


def export_name_count(media_type: str, name: str) -> int:
    """How many TMDB titles the ID export lists under this exact (original)
    name -- 0 when the export isn't loaded."""
    key = name_key(normalize(name))
    if key is None:
        return 0
    with _conn() as conn:
        return conn.execute("SELECT COUNT(*) FROM export_ids WHERE media_type=? AND name_key=?",
                            (media_type, key)).fetchone()[0]


def stats() -> list[dict]:
    out = []
    with _conn() as conn:
        for media_type in MEDIA_TYPES:
            row = conn.execute(
                """SELECT COUNT(*) AS entries,
                          SUM(release_date IS NOT NULL) AS with_release_date,
                          SUM(poster_path IS NOT NULL) AS with_poster,
                          SUM(top_cast IS NOT NULL) AS with_cast,
                          SUM(overview IS NOT NULL) AS with_overview,
                          SUM(vote_average IS NOT NULL) AS with_rating,
                          SUM(changed_at > fetched_at) AS stale,
                          MAX(fetched_at) AS last_fetched_at
                   FROM titles WHERE media_type=?""",
                (media_type,),
            ).fetchone()
            item = {k: (row[k] or 0) for k in row.keys()}
            item["last_fetched_at"] = row["last_fetched_at"]
            item["media_type"] = media_type
            item["export_ids"] = conn.execute(
                "SELECT COUNT(*) FROM export_ids WHERE media_type=?", (media_type,)
            ).fetchone()[0]
            item["export_date"] = get_meta(f"export_date:{media_type}")
            out.append(item)
    return out


def db_size_bytes() -> int:
    return sum(p.stat().st_size for p in DB_PATH.parent.glob(DB_PATH.name + "*") if p.is_file())
