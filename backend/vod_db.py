"""
VOD pool database — canonical Movies/TV Shows library, provider sources, and
smart category placements.

Design: content lives in a flat pool (movies, series/episodes), deduped from
whichever real providers offer it. Categories are our own curated/smart
labels, not provider-supplied ones. A single pool item can be placed into
multiple categories; since Dispatcharr's XC ingestion collapses same-account
entries that share the same (name, year), each placement beyond the first is
exported with an invisible zero-width-space marker appended to the name so
it lands as its own distinct catalog entry in Dispatcharr while still
resolving back to the same real provider source.
"""

import logging
import re
import secrets
import sqlite3
import time
from pathlib import Path

from config import DATA_DIR, get_config, get_refresh_settings, get_vod_xc_account_id

logger = logging.getLogger(__name__)

DB_PATH = DATA_DIR / "vod_db.sqlite"

# Offset export stream_ids well clear of any real provider's own ID range.
_EXPORT_STREAM_ID_BASE = 900_000_000
_SERIES_EXPORT_BASE = 910_000_000
_EPISODE_EXPORT_BASE = 920_000_000
_ZW_MARKER = "​"


def _connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    # WAL mode lets readers/writers proceed without blocking each other —
    # needed once something (e.g. a Plex library import) writes a real batch
    # while the background enrichment scheduler is also writing continuously;
    # under the default rollback-journal mode that contention raised "database
    # is locked". timeout=30 gives any remaining brief contention room to
    # retry instead of failing immediately.
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False, timeout=30.0)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = _connect()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS providers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            base_url TEXT NOT NULL,
            username TEXT NOT NULL,
            password TEXT NOT NULL,
            max_streams INTEGER NOT NULL DEFAULT 0,
            is_active INTEGER NOT NULL DEFAULT 1,
            priority INTEGER NOT NULL DEFAULT 0,
            dispatcharr_profile_id INTEGER,
            dispatcharr_live_account_id INTEGER,
            shared_connection_limit INTEGER,
            provider_type TEXT NOT NULL DEFAULT 'xc',
            created_at TEXT NOT NULL,
            updated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS movies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            year INTEGER,
            tmdb_id TEXT,
            imdb_id TEXT,
            genre TEXT,
            description TEXT,
            duration_secs INTEGER,
            poster_url TEXT,
            cast_list TEXT,
            director TEXT,
            country TEXT,
            is_adult INTEGER NOT NULL DEFAULT 0,
            is_adult_manual INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT,
            last_enriched_at TEXT
        );

        CREATE TABLE IF NOT EXISTS movie_sources (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            movie_id INTEGER NOT NULL REFERENCES movies(id) ON DELETE CASCADE,
            provider_id INTEGER NOT NULL REFERENCES providers(id) ON DELETE CASCADE,
            provider_stream_id TEXT NOT NULL,
            container_extension TEXT NOT NULL DEFAULT 'mp4',
            provider_category_name TEXT,
            plex_rating_key TEXT,
            added_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            UNIQUE(provider_id, provider_stream_id)
        );

        CREATE TABLE IF NOT EXISTS series (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            year INTEGER,
            tmdb_id TEXT,
            imdb_id TEXT,
            genre TEXT,
            description TEXT,
            poster_url TEXT,
            cast_list TEXT,
            director TEXT,
            country TEXT,
            is_adult INTEGER NOT NULL DEFAULT 0,
            is_adult_manual INTEGER NOT NULL DEFAULT 0,
            import_provider_id INTEGER REFERENCES providers(id),
            import_provider_series_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT,
            last_enriched_at TEXT
        );

        CREATE TABLE IF NOT EXISTS episodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            series_id INTEGER NOT NULL REFERENCES series(id) ON DELETE CASCADE,
            season_number INTEGER NOT NULL,
            episode_number INTEGER NOT NULL,
            name TEXT NOT NULL,
            description TEXT,
            duration_secs INTEGER,
            created_at TEXT NOT NULL,
            updated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS episode_sources (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            episode_id INTEGER NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
            provider_id INTEGER NOT NULL REFERENCES providers(id) ON DELETE CASCADE,
            provider_stream_id TEXT NOT NULL,
            container_extension TEXT NOT NULL DEFAULT 'mp4',
            provider_category_name TEXT,
            plex_rating_key TEXT,
            added_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            UNIQUE(provider_id, provider_stream_id)
        );

        CREATE TABLE IF NOT EXISTS categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            content_type TEXT NOT NULL CHECK(content_type IN ('movie', 'series')),
            is_smart INTEGER NOT NULL DEFAULT 0,
            rule_json TEXT,
            sync_source TEXT,
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS metadata_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content_type TEXT NOT NULL CHECK(content_type IN ('movie', 'series', 'both')),
            field TEXT NOT NULL,
            pattern TEXT NOT NULL,
            replacement TEXT NOT NULL DEFAULT '',
            is_active INTEGER NOT NULL DEFAULT 1,
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS movie_category_placements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            movie_id INTEGER NOT NULL REFERENCES movies(id) ON DELETE CASCADE,
            category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
            export_stream_id INTEGER NOT NULL UNIQUE,
            name_suffix TEXT NOT NULL DEFAULT '',
            UNIQUE(movie_id, category_id)
        );

        CREATE TABLE IF NOT EXISTS series_category_placements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            series_id INTEGER NOT NULL REFERENCES series(id) ON DELETE CASCADE,
            category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
            export_series_id INTEGER NOT NULL UNIQUE,
            name_suffix TEXT NOT NULL DEFAULT '',
            UNIQUE(series_id, category_id)
        );

        CREATE TABLE IF NOT EXISTS xc_clients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT NOT NULL,
            username TEXT NOT NULL UNIQUE,
            password TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            ip_allowlist TEXT,
            created_at TEXT NOT NULL,
            last_seen_at TEXT,
            last_seen_ip TEXT
        );

        CREATE TABLE IF NOT EXISTS dispatcharr_connections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT NOT NULL,
            url TEXT NOT NULL,
            token TEXT NOT NULL,
            vod_relay_account_id INTEGER,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS provider_live_accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider_id INTEGER NOT NULL REFERENCES providers(id) ON DELETE CASCADE,
            dispatcharr_connection_id INTEGER NOT NULL REFERENCES dispatcharr_connections(id) ON DELETE CASCADE,
            dispatcharr_account_id INTEGER NOT NULL,
            UNIQUE(provider_id, dispatcharr_connection_id)
        );

        CREATE TABLE IF NOT EXISTS provider_sync_profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider_id INTEGER NOT NULL REFERENCES providers(id) ON DELETE CASCADE,
            dispatcharr_connection_id INTEGER NOT NULL REFERENCES dispatcharr_connections(id) ON DELETE CASCADE,
            dispatcharr_profile_id INTEGER NOT NULL,
            UNIQUE(provider_id, dispatcharr_connection_id)
        );

        CREATE INDEX IF NOT EXISTS idx_movies_name_year ON movies(name, year);
        CREATE INDEX IF NOT EXISTS idx_series_name_year ON series(name, year);
        CREATE INDEX IF NOT EXISTS idx_episodes_series_season_ep ON episodes(series_id, season_number, episode_number);
    """)
    _commit_with_retry(conn)
    _migrate(conn)
    _migrate_primary_dispatcharr_connection(conn)
    conn.close()


def _migrate_primary_dispatcharr_connection(conn: sqlite3.Connection) -> None:
    """One-time: dispatcharr_connections used to be a single implicit
    connection (config.py's get_config() + get_vod_xc_account_id()) rather
    than a real list. If nothing's been added to the new table yet but that
    old single connection is configured, carry it over as the first row so
    existing setups (already-connected Dispatcharr instances) keep working
    exactly as before without the user needing to redo anything -- including
    each provider's already-synced dispatcharr_profile_id (providers.
    dispatcharr_profile_id was also a single implicit value; carried into
    provider_sync_profiles for this same connection, or the next sync would
    have re-POSTed a duplicate profile instead of PATCHing the existing one)."""
    existing = conn.execute("SELECT COUNT(*) c FROM dispatcharr_connections").fetchone()["c"]
    if existing > 0:
        return
    url, token = get_config()
    if not url or not token:
        return
    cur = conn.execute(
        "INSERT INTO dispatcharr_connections (label, url, token, vod_relay_account_id, created_at) VALUES (?,?,?,?,?)",
        ("Primary", url, token, get_vod_xc_account_id(), _now()),
    )
    connection_id = cur.lastrowid
    for row in conn.execute("SELECT id, dispatcharr_profile_id FROM providers WHERE dispatcharr_profile_id IS NOT NULL").fetchall():
        conn.execute(
            "INSERT INTO provider_sync_profiles (provider_id, dispatcharr_connection_id, dispatcharr_profile_id) VALUES (?,?,?)",
            (row["id"], connection_id, row["dispatcharr_profile_id"]),
        )
    _commit_with_retry(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns to pre-existing tables that predate them. CREATE TABLE IF
    NOT EXISTS above only helps fresh databases."""
    migrations = [
        ("movies", "last_enriched_at", "TEXT"),
        ("movies", "cast_list", "TEXT"),
        ("movies", "director", "TEXT"),
        ("movies", "country", "TEXT"),
        ("series", "last_enriched_at", "TEXT"),
        ("series", "cast_list", "TEXT"),
        ("series", "director", "TEXT"),
        ("series", "country", "TEXT"),
        ("series", "import_provider_id", "INTEGER"),
        ("series", "import_provider_series_id", "TEXT"),
        ("movie_sources", "provider_category_name", "TEXT"),
        ("episode_sources", "provider_category_name", "TEXT"),
        ("providers", "priority", "INTEGER NOT NULL DEFAULT 0"),
        ("movies", "is_adult", "INTEGER NOT NULL DEFAULT 0"),
        ("series", "is_adult", "INTEGER NOT NULL DEFAULT 0"),
        ("movies", "is_adult_manual", "INTEGER NOT NULL DEFAULT 0"),
        ("series", "is_adult_manual", "INTEGER NOT NULL DEFAULT 0"),
        ("categories", "sync_source", "TEXT"),
        ("providers", "dispatcharr_live_account_id", "INTEGER"),
        ("providers", "shared_connection_limit", "INTEGER"),
        ("providers", "provider_type", "TEXT NOT NULL DEFAULT 'xc'"),
        ("movie_sources", "plex_rating_key", "TEXT"),
        ("episode_sources", "plex_rating_key", "TEXT"),
        ("movies", "needs_year_review", "INTEGER NOT NULL DEFAULT 0"),
        ("series", "needs_year_review", "INTEGER NOT NULL DEFAULT 0"),
        ("providers", "custom_user_agent", "TEXT"),
        ("providers", "last_catalog_refresh_at", "TEXT"),
        ("xc_clients", "category_allowlist", "TEXT"),
        ("categories", "ai_description", "TEXT"),
    ]
    for table, column, coltype in migrations:
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
    _commit_with_retry(conn)


def _now() -> str:
    return str(time.time())


def _commit_with_retry(conn: sqlite3.Connection, retries: int = 5) -> None:
    """Retries a commit through transient 'database is locked' contention —
    needed once something writes a real batch (e.g. a Plex library import)
    while the background enrichment scheduler is also writing continuously.
    A single very long transaction is a bad neighbor to that scheduler's
    frequent short writes, so bulk import functions call this every N items
    rather than once at the very end (see bulk_import_plex_movies/series)."""
    for attempt in range(retries):
        try:
            conn.commit()
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() or attempt == retries - 1:
                raise
            time.sleep(0.5 * (attempt + 1))


# ── Providers ────────────────────────────────────────────────────────────────

def upsert_provider(
    name: str, base_url: str, username: str, password: str, max_streams: int = 0, priority: int = 0,
    provider_type: str = "xc",
) -> int:
    conn = _connect()
    row = conn.execute("SELECT id FROM providers WHERE name = ?", (name,)).fetchone()
    if row:
        conn.execute(
            "UPDATE providers SET base_url=?, username=?, password=?, max_streams=?, priority=?, provider_type=?, updated_at=? WHERE id=?",
            (base_url, username, password, max_streams, priority, provider_type, _now(), row["id"]),
        )
        provider_id = row["id"]
    else:
        cur = conn.execute(
            "INSERT INTO providers (name, base_url, username, password, max_streams, priority, provider_type, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (name, base_url, username, password, max_streams, priority, provider_type, _now()),
        )
        provider_id = cur.lastrowid
    _commit_with_retry(conn)
    conn.close()
    return provider_id


def set_provider_priority(provider_id: int, priority: int) -> None:
    conn = _connect()
    conn.execute("UPDATE providers SET priority=?, updated_at=? WHERE id=?", (priority, _now(), provider_id))
    _commit_with_retry(conn)
    conn.close()


def set_provider_name(provider_id: int, name: str) -> None:
    conn = _connect()
    conn.execute("UPDATE providers SET name=?, updated_at=? WHERE id=?", (name, _now(), provider_id))
    _commit_with_retry(conn)
    conn.close()


def set_provider_base_url(provider_id: int, base_url: str) -> None:
    conn = _connect()
    conn.execute("UPDATE providers SET base_url=?, updated_at=? WHERE id=?", (base_url, _now(), provider_id))
    _commit_with_retry(conn)
    conn.close()


def set_provider_max_streams(provider_id: int, max_streams: int) -> None:
    conn = _connect()
    conn.execute("UPDATE providers SET max_streams=?, updated_at=? WHERE id=?", (max_streams, _now(), provider_id))
    _commit_with_retry(conn)
    conn.close()


def set_provider_shared_limit(provider_id: int, shared_connection_limit: int | None) -> None:
    """The real provider's true total connection cap, shared across every
    live-TV account on any Dispatcharr instance plus our own VOD streaming
    -- see xc_server.py's _has_capacity(). Which specific live-TV accounts
    count toward it is managed separately (provider_live_accounts, since a
    provider can have one on more than one Dispatcharr instance)."""
    conn = _connect()
    conn.execute(
        "UPDATE providers SET shared_connection_limit=?, updated_at=? WHERE id=?",
        (shared_connection_limit, _now(), provider_id),
    )
    _commit_with_retry(conn)
    conn.close()


def set_provider_custom_user_agent(provider_id: int, custom_user_agent: str | None) -> None:
    """Overrides the default browser User-Agent (see vod_importer.py's
    _UPSTREAM_HEADERS) for just this provider -- most providers work fine
    with the shared default; this is only needed if one turns out to be
    pickier (blocks even a normal browser UA, or wants something else
    entirely). None/empty clears the override and falls back to the default."""
    conn = _connect()
    conn.execute(
        "UPDATE providers SET custom_user_agent=?, updated_at=? WHERE id=?",
        (custom_user_agent or None, _now(), provider_id),
    )
    _commit_with_retry(conn)
    conn.close()


def set_provider_dispatcharr_profile(provider_id: int, profile_id: int) -> None:
    conn = _connect()
    conn.execute("UPDATE providers SET dispatcharr_profile_id=?, updated_at=? WHERE id=?", (profile_id, _now(), provider_id))
    _commit_with_retry(conn)
    conn.close()


def get_provider(provider_id: int) -> dict | None:
    conn = _connect()
    row = conn.execute("SELECT * FROM providers WHERE id=?", (provider_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def list_providers() -> list[dict]:
    conn = _connect()
    rows = [dict(r) for r in conn.execute("SELECT * FROM providers ORDER BY name").fetchall()]
    movie_counts = {r["provider_id"]: r["c"] for r in conn.execute(
        "SELECT provider_id, COUNT(*) c FROM movie_sources GROUP BY provider_id"
    ).fetchall()}
    # Distinct series with at least one episode actually sourced from this
    # provider — not "series this provider happened to create the row for"
    # (series.import_provider_id), which undercounts any series a later
    # provider's episodes merged into but didn't originally create. Reported
    # alongside the raw episode count (a different, larger number by design —
    # e.g. one series with 62 episodes contributes 1 to series_count but 62
    # to episode_count) so both are visible instead of one figure standing
    # in for two different things.
    series_counts = {r["provider_id"]: r["c"] for r in conn.execute("""
        SELECT es.provider_id, COUNT(DISTINCT e.series_id) c
        FROM episode_sources es
        JOIN episodes e ON e.id = es.episode_id
        GROUP BY es.provider_id
    """).fetchall()}
    episode_counts = {r["provider_id"]: r["c"] for r in conn.execute(
        "SELECT provider_id, COUNT(*) c FROM episode_sources GROUP BY provider_id"
    ).fetchall()}
    synced_counts = {r["provider_id"]: r["c"] for r in conn.execute(
        "SELECT provider_id, COUNT(*) c FROM provider_sync_profiles GROUP BY provider_id"
    ).fetchall()}
    live_account_counts = {r["provider_id"]: r["c"] for r in conn.execute(
        "SELECT provider_id, COUNT(*) c FROM provider_live_accounts GROUP BY provider_id"
    ).fetchall()}
    conn.close()
    for p in rows:
        p["movie_count"] = movie_counts.get(p["id"], 0)
        p["series_count"] = series_counts.get(p["id"], 0)
        p["episode_count"] = episode_counts.get(p["id"], 0)
        p["synced_connection_count"] = synced_counts.get(p["id"], 0)
        p["live_account_count"] = live_account_counts.get(p["id"], 0)
    return rows


def set_provider_active(provider_id: int, is_active: bool) -> None:
    conn = _connect()
    conn.execute("UPDATE providers SET is_active=?, updated_at=? WHERE id=?", (int(is_active), _now(), provider_id))
    _commit_with_retry(conn)
    conn.close()


def _purge_if_sourceless_movie(conn: sqlite3.Connection, movie_id: int) -> None:
    """A movie with zero sources from any provider can't actually be played,
    but would still show up as if it were real, available content in
    Dispatcharr's catalog and any downstream IPTV player -- worse than not
    listing it at all. Called after removing what might have been a movie's
    last source, whether that's one source, or a whole provider's worth."""
    remaining = conn.execute("SELECT COUNT(*) c FROM movie_sources WHERE movie_id=?", (movie_id,)).fetchone()["c"]
    if remaining == 0:
        conn.execute("DELETE FROM movies WHERE id=?", (movie_id,))


def _purge_if_sourceless_episode(conn: sqlite3.Connection, episode_id: int) -> None:
    """A specific episode can go sourceless (its one source deleted, or
    belonged to a now-deleted provider) while the series it belongs to
    still has other episodes with real sources from other providers -- the
    series survives, but this one episode is dead weight, same reasoning as
    _purge_if_sourceless_movie."""
    remaining = conn.execute("SELECT COUNT(*) c FROM episode_sources WHERE episode_id=?", (episode_id,)).fetchone()["c"]
    if remaining == 0:
        conn.execute("DELETE FROM episodes WHERE id=?", (episode_id,))


def _purge_if_sourceless_series(conn: sqlite3.Connection, series_id: int, orphaned_provider_id: int | None = None) -> None:
    """Series equivalent of _purge_if_sourceless_movie -- deletes the whole
    series only once none of its episodes have any source left at all (see
    _purge_if_sourceless_episode for the per-episode version). If the series
    survives (still has sources from other providers) but its cached
    import_provider_id -- the "ask this provider for episode details"
    reference used by enrich_series, a plain column with no real FK, not a
    real source record -- pointed at the provider that just lost its
    sources, clear it too, so a later enrich attempt fails cleanly instead
    of silently hitting a provider that's no longer there."""
    remaining = conn.execute("""
        SELECT COUNT(*) c FROM episode_sources es
        JOIN episodes e ON e.id = es.episode_id
        WHERE e.series_id=?
    """, (series_id,)).fetchone()["c"]
    if remaining == 0:
        conn.execute("DELETE FROM series WHERE id=?", (series_id,))
    elif orphaned_provider_id is not None:
        conn.execute(
            "UPDATE series SET import_provider_id=NULL, import_provider_series_id=NULL WHERE id=? AND import_provider_id=?",
            (series_id, orphaned_provider_id),
        )


def delete_provider(provider_id: int) -> None:
    """Hard delete. movie_sources/episode_sources for this provider cascade
    via FK (ON DELETE CASCADE). Anything left with zero sources from any
    provider afterward is purged too -- see _purge_if_sourceless_movie/
    episode/series."""
    conn = _connect()

    # Capture affected movies/episodes/series before the cascade delete
    # removes the only signal (their sources) that would tell us which ones
    # to check.
    affected_movie_ids = [r["movie_id"] for r in conn.execute(
        "SELECT DISTINCT movie_id FROM movie_sources WHERE provider_id=?", (provider_id,)
    ).fetchall()]
    affected_episode_rows = conn.execute("""
        SELECT DISTINCT e.id AS episode_id, e.series_id FROM episode_sources es
        JOIN episodes e ON e.id = es.episode_id
        WHERE es.provider_id=?
    """, (provider_id,)).fetchall()

    conn.execute("DELETE FROM providers WHERE id=?", (provider_id,))

    for movie_id in affected_movie_ids:
        _purge_if_sourceless_movie(conn, movie_id)
    affected_series_ids = {r["series_id"] for r in affected_episode_rows}
    for row in affected_episode_rows:
        _purge_if_sourceless_episode(conn, row["episode_id"])
    for series_id in affected_series_ids:
        _purge_if_sourceless_series(conn, series_id, orphaned_provider_id=provider_id)

    _commit_with_retry(conn)
    conn.close()


# ── Orphan checker ───────────────────────────────────────────────────────────
# Self-service version of the manual investigation that found the original
# bug this exists to prevent recurring: a provider getting deleted (or, more
# subtly, a source silently losing its movie/episode association -- see the
# ON CONFLICT fixes on the bulk_import_* functions above) can leave dead
# rows behind that _purge_if_sourceless_* would have caught at the time, but
# only if delete_provider/delete_movie_source/delete_episode_source was the
# path taken. This re-derives the same two categories from scratch across
# the whole pool, for whatever slips through (a bug elsewhere, a manual DB
# edit, an upgrade from before these functions existed) rather than assuming
# every future gap will go through the choke points already covered.
#
# Deliberately does NOT flag "series with zero episodes yet" as an orphan --
# that's the overwhelming majority of any freshly bulk-imported pool (XC
# episodes are fetched lazily per-series, on demand, by design) and is
# completely normal, not broken. Only a series whose cached
# import_provider_id points at a provider that no longer exists at all is
# genuinely unfixable and worth flagging.

def find_orphans() -> dict:
    conn = _connect()
    valid_provider_ids = {r["id"] for r in conn.execute("SELECT id FROM providers").fetchall()}

    orphaned_series = [dict(r) for r in conn.execute("SELECT id, name, import_provider_id FROM series").fetchall()
                        if r["import_provider_id"] is None or r["import_provider_id"] not in valid_provider_ids]
    sourceless_movies = [dict(r) for r in conn.execute("""
        SELECT m.id, m.name FROM movies m
        LEFT JOIN movie_sources ms ON ms.movie_id = m.id
        WHERE ms.id IS NULL
    """).fetchall()]
    sourceless_episodes = [dict(r) for r in conn.execute("""
        SELECT e.id, e.series_id, e.name FROM episodes e
        LEFT JOIN episode_sources es ON es.episode_id = e.id
        WHERE es.id IS NULL
    """).fetchall()]
    conn.close()

    return {
        "orphaned_series": {"count": len(orphaned_series), "sample": orphaned_series[:20]},
        "sourceless_movies": {"count": len(sourceless_movies), "sample": sourceless_movies[:20]},
        "sourceless_episodes": {"count": len(sourceless_episodes), "sample": sourceless_episodes[:20]},
    }


def purge_orphans() -> dict:
    conn = _connect()
    valid_provider_ids = {r["id"] for r in conn.execute("SELECT id FROM providers").fetchall()}

    orphaned_series_ids = [r["id"] for r in conn.execute("SELECT id, import_provider_id FROM series").fetchall()
                            if r["import_provider_id"] is None or r["import_provider_id"] not in valid_provider_ids]
    sourceless_movie_ids = [r["id"] for r in conn.execute("""
        SELECT m.id FROM movies m LEFT JOIN movie_sources ms ON ms.movie_id = m.id WHERE ms.id IS NULL
    """).fetchall()]
    # Episodes belonging to a series about to be deleted anyway don't need a
    # separate delete -- ON DELETE CASCADE handles them. Only ones inside an
    # otherwise-healthy series need to be purged individually.
    sourceless_episode_ids = [r["id"] for r in conn.execute("""
        SELECT e.id FROM episodes e
        LEFT JOIN episode_sources es ON es.episode_id = e.id
        WHERE es.id IS NULL AND e.series_id NOT IN ({})
    """.format(",".join("?" * len(orphaned_series_ids)) if orphaned_series_ids else "NULL"),
        orphaned_series_ids,
    ).fetchall()]

    for sid in orphaned_series_ids:
        conn.execute("DELETE FROM series WHERE id=?", (sid,))
    for mid in sourceless_movie_ids:
        conn.execute("DELETE FROM movies WHERE id=?", (mid,))
    for eid in sourceless_episode_ids:
        conn.execute("DELETE FROM episodes WHERE id=?", (eid,))

    _commit_with_retry(conn)
    conn.close()
    return {
        "series_deleted": len(orphaned_series_ids),
        "movies_deleted": len(sourceless_movie_ids),
        "episodes_deleted": len(sourceless_episode_ids),
    }


# ── Duplicate finder (punctuation/whitespace-only name variants) ───────────
# Import matching (bulk_import_movies/bulk_import_series, upsert_movie) keys
# on exact name+year -- a provider that formats the same title slightly
# differently ("Title" vs "Title:") creates a second real pool entry instead
# of matching the existing one (a real case: "#AMFAD All My Friends Are
# Dead" vs "#AMFAD: All My Friends Are Dead", both 2024, split into two
# rows). Same review-before-merge trust pattern as Orphan Checker/Needs
# Review, not an automatic pass -- punctuation normalization is
# high-confidence but not risk-free, and a bad auto-merge is much harder to
# notice/undo than a bad auto-delete.

_DUPLICATE_STRIP_RE = re.compile(r"[:;,.'\"’‘“”\-–—]")
_DUPLICATE_WS_RE = re.compile(r"\s+")


def _normalize_title_for_dedup(name: str) -> str:
    stripped = _DUPLICATE_STRIP_RE.sub("", name)
    return _DUPLICATE_WS_RE.sub(" ", stripped).strip().lower()


def find_duplicate_groups(content_type: str) -> list[dict]:
    """Groups same-year pool entries whose names are identical once cosmetic
    punctuation/whitespace is stripped. Only years we're confident about
    (year IS NOT NULL) -- pairing on name alone would be a much weaker
    signal and belongs to needs_year_review instead, not this scan."""
    table = "movies" if content_type == "movie" else "series"
    id_col = "movie_id" if content_type == "movie" else "series_id"
    placements_table = "movie_category_placements" if content_type == "movie" else "series_category_placements"

    conn = _connect()
    rows = conn.execute(f"SELECT id, name, year FROM {table} WHERE year IS NOT NULL").fetchall()

    groups: dict[tuple[str, int], list[dict]] = {}
    for r in rows:
        key = (_normalize_title_for_dedup(r["name"]), r["year"])
        groups.setdefault(key, []).append({"id": r["id"], "name": r["name"]})

    # Only real punctuation-variant duplicates -- a group of 2+ rows that
    # already share the exact same name shouldn't exist (import matching
    # would have collapsed them), so require at least 2 *distinct* spellings.
    candidate_groups = [items for items in groups.values() if len(items) >= 2 and len({i["name"] for i in items}) >= 2]
    if not candidate_groups:
        conn.close()
        return []

    all_ids = [i["id"] for items in candidate_groups for i in items]
    placeholders = ",".join("?" for _ in all_ids)

    if content_type == "movie":
        src_counts = conn.execute(
            f"SELECT movie_id AS id, COUNT(*) c FROM movie_sources WHERE movie_id IN ({placeholders}) GROUP BY movie_id",
            all_ids,
        ).fetchall()
    else:
        src_counts = conn.execute(f"""
            SELECT e.series_id AS id, COUNT(*) c FROM episode_sources es
            JOIN episodes e ON e.id = es.episode_id
            WHERE e.series_id IN ({placeholders}) GROUP BY e.series_id
        """, all_ids).fetchall()
    src_count_by_id = {r["id"]: r["c"] for r in src_counts}

    cat_counts = conn.execute(
        f"SELECT {id_col} AS id, COUNT(*) c FROM {placements_table} WHERE {id_col} IN ({placeholders}) GROUP BY {id_col}",
        all_ids,
    ).fetchall()
    cat_count_by_id = {r["id"]: r["c"] for r in cat_counts}
    conn.close()

    result = []
    for items in candidate_groups:
        for i in items:
            i["source_count"] = src_count_by_id.get(i["id"], 0)
            i["category_count"] = cat_count_by_id.get(i["id"], 0)
        # Most-sourced/most-placed first -- the obvious default "keep" pick.
        items.sort(key=lambda i: (-i["source_count"], -i["category_count"]))
        result.append({"items": items})
    result.sort(key=lambda g: -sum(i["source_count"] for i in g["items"]))
    return result


def merge_duplicate_group(content_type: str, keep_id: int, merge_ids: list[int]) -> dict:
    merged = 0
    for mid in merge_ids:
        if mid == keep_id:
            continue
        if content_type == "movie":
            merge_movie(mid, keep_id)
        else:
            merge_series(mid, keep_id)
        merged += 1
    return {"kept_id": keep_id, "merged_count": merged}


# ── XC clients ───────────────────────────────────────────────────────────────
# One credential pair per downstream Dispatcharr instance (or any other XC
# client) allowed to pull this pool. Auto-generated, high-entropy username/
# password rather than anything user-chosen — this is the only thing standing
# between the XC catalog and the open internet if this server is ever reached
# from outside a trusted network, so it needs to be as strong as a real API
# key, not a typed password. See xc_server.py's _authenticate.

def _generate_xc_username() -> str:
    return f"vm-{secrets.token_hex(4)}"


def _generate_xc_password() -> str:
    return secrets.token_urlsafe(32)


def create_xc_client(label: str, ip_allowlist: str | None = None) -> dict:
    username = _generate_xc_username()
    password = _generate_xc_password()
    conn = _connect()
    cur = conn.execute(
        "INSERT INTO xc_clients (label, username, password, enabled, ip_allowlist, created_at) VALUES (?,?,?,1,?,?)",
        (label, username, password, ip_allowlist, _now()),
    )
    client_id = cur.lastrowid
    _commit_with_retry(conn)
    conn.close()
    return get_xc_client(client_id)


def list_xc_clients() -> list[dict]:
    conn = _connect()
    rows = [dict(r) for r in conn.execute("SELECT * FROM xc_clients ORDER BY created_at ASC").fetchall()]
    conn.close()
    return rows


def list_enabled_xc_clients() -> list[dict]:
    conn = _connect()
    rows = [dict(r) for r in conn.execute("SELECT * FROM xc_clients WHERE enabled=1 ORDER BY created_at ASC").fetchall()]
    conn.close()
    return rows


def get_xc_client(client_id: int) -> dict | None:
    conn = _connect()
    row = conn.execute("SELECT * FROM xc_clients WHERE id=?", (client_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_default_xc_client() -> dict | None:
    """The oldest enabled client -- used only where a single representative
    credential pair is needed (e.g. building a copy/preview URL in the UI),
    not for real auth decisions. Any enabled client's credentials work
    identically for that purpose since they all see the same pool."""
    conn = _connect()
    row = conn.execute("SELECT * FROM xc_clients WHERE enabled=1 ORDER BY created_at ASC LIMIT 1").fetchone()
    conn.close()
    return dict(row) if row else None


def update_xc_client(
    client_id: int, label: str | None = None, enabled: bool | None = None,
    ip_allowlist: str | None = None, clear_ip_allowlist: bool = False,
    category_allowlist: str | None = None, clear_category_allowlist: bool = False,
) -> None:
    conn = _connect()
    if label is not None:
        conn.execute("UPDATE xc_clients SET label=? WHERE id=?", (label, client_id))
    if enabled is not None:
        conn.execute("UPDATE xc_clients SET enabled=? WHERE id=?", (int(enabled), client_id))
    if clear_ip_allowlist:
        conn.execute("UPDATE xc_clients SET ip_allowlist=NULL WHERE id=?", (client_id,))
    elif ip_allowlist is not None:
        conn.execute("UPDATE xc_clients SET ip_allowlist=? WHERE id=?", (ip_allowlist, client_id))
    if clear_category_allowlist:
        conn.execute("UPDATE xc_clients SET category_allowlist=NULL WHERE id=?", (client_id,))
    elif category_allowlist is not None:
        conn.execute("UPDATE xc_clients SET category_allowlist=? WHERE id=?", (category_allowlist, client_id))
    _commit_with_retry(conn)
    conn.close()


def regenerate_xc_client_secret(client_id: int) -> dict:
    password = _generate_xc_password()
    conn = _connect()
    conn.execute("UPDATE xc_clients SET password=? WHERE id=?", (password, client_id))
    _commit_with_retry(conn)
    conn.close()
    return get_xc_client(client_id)


def delete_xc_client(client_id: int) -> None:
    conn = _connect()
    conn.execute("DELETE FROM xc_clients WHERE id=?", (client_id,))
    _commit_with_retry(conn)
    conn.close()


def record_xc_client_seen(client_id: int, ip: str) -> None:
    conn = _connect()
    conn.execute("UPDATE xc_clients SET last_seen_at=?, last_seen_ip=? WHERE id=?", (_now(), ip, client_id))
    _commit_with_retry(conn)
    conn.close()


# ── Dispatcharr connections ─────────────────────────────────────────────────
# The other side of running against multiple Dispatcharr instances (see
# xc_clients above, which is who's allowed to *pull from* VOD Manager): this
# is who VOD Manager itself *reaches out to*, for two purposes that used to
# assume there was only ever one such instance --
#   1. vod_sync.py pushes each provider's max_streams into a Dispatcharr
#      account's connection-limit profiles (vod_relay_account_id: which
#      account on this connection is the one pointing back at VOD Manager).
#   2. xc_server.py's shared-connection-limit coordination (_has_capacity)
#      checks live-TV viewer counts against a real provider's total cap --
#      see provider_live_accounts below, since a single real provider can
#      have its own separate native live-TV account on more than one
#      Dispatcharr instance, all drawing from the same real connection pool.

def create_dispatcharr_connection(label: str, url: str, token: str) -> int:
    conn = _connect()
    cur = conn.execute(
        "INSERT INTO dispatcharr_connections (label, url, token, created_at) VALUES (?,?,?,?)",
        (label, url.rstrip("/"), token, _now()),
    )
    connection_id = cur.lastrowid
    _commit_with_retry(conn)
    conn.close()
    return connection_id


def list_dispatcharr_connections() -> list[dict]:
    conn = _connect()
    rows = [dict(r) for r in conn.execute("SELECT * FROM dispatcharr_connections ORDER BY created_at ASC").fetchall()]
    conn.close()
    return rows


def get_dispatcharr_connection(connection_id: int) -> dict | None:
    conn = _connect()
    row = conn.execute("SELECT * FROM dispatcharr_connections WHERE id=?", (connection_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def update_dispatcharr_connection(
    connection_id: int, label: str | None = None, url: str | None = None,
    token: str | None = None, vod_relay_account_id: int | None = None,
    clear_vod_relay_account_id: bool = False,
) -> None:
    conn = _connect()
    if label is not None:
        conn.execute("UPDATE dispatcharr_connections SET label=? WHERE id=?", (label, connection_id))
    if url is not None:
        conn.execute("UPDATE dispatcharr_connections SET url=? WHERE id=?", (url.rstrip("/"), connection_id))
    if token is not None:
        conn.execute("UPDATE dispatcharr_connections SET token=? WHERE id=?", (token, connection_id))
    if clear_vod_relay_account_id:
        conn.execute("UPDATE dispatcharr_connections SET vod_relay_account_id=NULL WHERE id=?", (connection_id,))
    elif vod_relay_account_id is not None:
        conn.execute("UPDATE dispatcharr_connections SET vod_relay_account_id=? WHERE id=?", (vod_relay_account_id, connection_id))
    _commit_with_retry(conn)
    conn.close()


def delete_dispatcharr_connection(connection_id: int) -> None:
    conn = _connect()
    conn.execute("DELETE FROM dispatcharr_connections WHERE id=?", (connection_id,))
    _commit_with_retry(conn)
    conn.close()


# ── Provider live-TV accounts (for shared connection-limit coordination) ────

def list_provider_live_accounts(provider_id: int) -> list[dict]:
    conn = _connect()
    rows = [dict(r) for r in conn.execute("""
        SELECT pla.*, dc.label AS connection_label FROM provider_live_accounts pla
        JOIN dispatcharr_connections dc ON dc.id = pla.dispatcharr_connection_id
        WHERE pla.provider_id=?
        ORDER BY dc.label
    """, (provider_id,)).fetchall()]
    conn.close()
    return rows


def set_provider_live_account(provider_id: int, connection_id: int, account_id: int) -> int:
    """Upsert -- one row per (provider, connection) pair; setting it again
    for the same connection just updates the account id."""
    conn = _connect()
    cur = conn.execute(
        """INSERT INTO provider_live_accounts (provider_id, dispatcharr_connection_id, dispatcharr_account_id)
           VALUES (?,?,?)
           ON CONFLICT(provider_id, dispatcharr_connection_id) DO UPDATE SET dispatcharr_account_id=excluded.dispatcharr_account_id""",
        (provider_id, connection_id, account_id),
    )
    _commit_with_retry(conn)
    row_id = conn.execute(
        "SELECT id FROM provider_live_accounts WHERE provider_id=? AND dispatcharr_connection_id=?",
        (provider_id, connection_id),
    ).fetchone()["id"]
    conn.close()
    return row_id


def remove_provider_live_account(link_id: int) -> None:
    conn = _connect()
    conn.execute("DELETE FROM provider_live_accounts WHERE id=?", (link_id,))
    _commit_with_retry(conn)
    conn.close()


# ── Provider sync profiles (per-connection Dispatcharr profile id) ──────────
# Which Dispatcharr profile object (on a given connection's VOD-relay
# account) represents this provider's max_streams -- needed per-connection
# since syncing to N Dispatcharr instances means N separate profile objects,
# not one shared id.

def get_provider_sync_profile(provider_id: int, connection_id: int) -> int | None:
    conn = _connect()
    row = conn.execute(
        "SELECT dispatcharr_profile_id FROM provider_sync_profiles WHERE provider_id=? AND dispatcharr_connection_id=?",
        (provider_id, connection_id),
    ).fetchone()
    conn.close()
    return row["dispatcharr_profile_id"] if row else None


def set_provider_sync_profile(provider_id: int, connection_id: int, profile_id: int) -> None:
    conn = _connect()
    conn.execute(
        """INSERT INTO provider_sync_profiles (provider_id, dispatcharr_connection_id, dispatcharr_profile_id)
           VALUES (?,?,?)
           ON CONFLICT(provider_id, dispatcharr_connection_id) DO UPDATE SET dispatcharr_profile_id=excluded.dispatcharr_profile_id""",
        (provider_id, connection_id, profile_id),
    )
    _commit_with_retry(conn)
    conn.close()


# ── Categories ───────────────────────────────────────────────────────────────

def upsert_category(
    name: str, content_type: str, is_smart: bool = False, sort_order: int = 0,
    rule_json: str | None = None,
) -> int:
    conn = _connect()
    row = conn.execute("SELECT id FROM categories WHERE name = ? AND content_type = ?", (name, content_type)).fetchone()
    if row:
        category_id = row["id"]
        conn.execute(
            "UPDATE categories SET content_type=?, is_smart=?, sort_order=?, rule_json=? WHERE id=?",
            (content_type, int(is_smart), sort_order, rule_json, category_id),
        )
    else:
        cur = conn.execute(
            "INSERT INTO categories (name, content_type, is_smart, sort_order, rule_json, created_at) VALUES (?,?,?,?,?,?)",
            (name, content_type, int(is_smart), sort_order, rule_json, _now()),
        )
        category_id = cur.lastrowid
    _commit_with_retry(conn)
    conn.close()
    return category_id


def get_category(category_id: int) -> dict | None:
    conn = _connect()
    row = conn.execute("SELECT * FROM categories WHERE id=?", (category_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def delete_category(category_id: int) -> None:
    """Hard delete. movie_category_placements/series_category_placements for
    this category cascade via FK — the movies/series themselves are untouched,
    just no longer placed in this category."""
    conn = _connect()
    conn.execute("DELETE FROM categories WHERE id=?", (category_id,))
    _commit_with_retry(conn)
    conn.close()


def set_category_sort_order(category_id: int, sort_order: int) -> None:
    conn = _connect()
    conn.execute("UPDATE categories SET sort_order=? WHERE id=?", (sort_order, category_id))
    _commit_with_retry(conn)
    conn.close()


def set_category_name(category_id: int, name: str) -> None:
    conn = _connect()
    conn.execute("UPDATE categories SET name=? WHERE id=?", (name, category_id))
    _commit_with_retry(conn)
    conn.close()


def set_category_ai_description(category_id: int, ai_description: str | None) -> None:
    """Persisted so a re-run of AI Evaluate (see ai_assist.py) doesn't require
    re-typing the description each time -- same pattern as sync_source for
    TMDB Lists categories."""
    conn = _connect()
    conn.execute("UPDATE categories SET ai_description=? WHERE id=?", (ai_description, category_id))
    _commit_with_retry(conn)
    conn.close()


def set_category_sync_source(category_id: int, sync_source: str | None) -> None:
    """sync_source e.g. 'tmdb_list:1234567' — see tmdb_sync.py for the actual
    fetch/match/place logic that reads this."""
    conn = _connect()
    conn.execute("UPDATE categories SET sync_source=? WHERE id=?", (sync_source, category_id))
    _commit_with_retry(conn)
    conn.close()


def list_sync_categories() -> list[dict]:
    """All categories with a sync_source configured — what the scheduled/manual sync walks."""
    conn = _connect()
    rows = conn.execute("SELECT * FROM categories WHERE sync_source IS NOT NULL AND sync_source != ''").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_movie_by_tmdb_id(tmdb_id: str) -> dict | None:
    conn = _connect()
    row = conn.execute("SELECT * FROM movies WHERE tmdb_id=?", (str(tmdb_id),)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_series_by_tmdb_id(tmdb_id: str) -> dict | None:
    conn = _connect()
    row = conn.execute("SELECT * FROM series WHERE tmdb_id=?", (str(tmdb_id),)).fetchone()
    conn.close()
    return dict(row) if row else None


def list_categories(content_type: str | None = None) -> list[dict]:
    conn = _connect()
    if content_type:
        rows = conn.execute(
            "SELECT * FROM categories WHERE content_type=? ORDER BY sort_order, name", (content_type,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM categories ORDER BY sort_order, name").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_movie_category_ids(movie_id: int) -> list[int]:
    """Which categories a movie is placed in -- used by xc_server's
    per-client category allowlist to decide whether a restricted client may
    reach a movie via a route (e.g. preview) that isn't already filtered by
    a specific category placement's export id."""
    conn = _connect()
    rows = conn.execute("SELECT category_id FROM movie_category_placements WHERE movie_id=?", (movie_id,)).fetchall()
    conn.close()
    return [r["category_id"] for r in rows]


def get_series_category_ids(series_id: int) -> list[int]:
    """Series equivalent of get_movie_category_ids — see there."""
    conn = _connect()
    rows = conn.execute("SELECT category_id FROM series_category_placements WHERE series_id=?", (series_id,)).fetchall()
    conn.close()
    return [r["category_id"] for r in rows]


# ── Movies ───────────────────────────────────────────────────────────────────

def upsert_movie(name: str, year: int | None = None, **fields) -> int:
    conn = _connect()

    def _insert(needs_review: int = 0) -> int:
        cols = ["name", "year", "needs_year_review", *fields.keys()]
        vals = [name, year, needs_review, *fields.values()]
        placeholders = ", ".join("?" for _ in cols)
        cur = conn.execute(
            f"INSERT INTO movies ({', '.join(cols)}, created_at) VALUES ({placeholders}, ?)",
            (*vals, _now()),
        )
        return cur.lastrowid

    def _update(movie_id: int) -> None:
        if fields:
            sets = ", ".join(f"{k}=?" for k in fields)
            conn.execute(f"UPDATE movies SET {sets}, updated_at=? WHERE id=?", (*fields.values(), _now(), movie_id))

    row = conn.execute("SELECT id FROM movies WHERE name = ? AND year IS ?", (name, year)).fetchone()
    if row:
        movie_id = row["id"]
        _update(movie_id)
    elif year is None:
        # No exact (name, NULL) row exists yet. Rather than blindly create a
        # fresh row that might just be an unlabeled duplicate of something
        # already in the pool, look for existing candidates by name alone.
        # Exactly one -> merge into it (almost certainly the same title,
        # just missing year metadata from this particular source). Two or
        # more -> can't tell which one it is, so create a new row but flag
        # it for a human to resolve rather than silently guessing wrong.
        candidates = conn.execute("SELECT id FROM movies WHERE name = ?", (name,)).fetchall()
        if len(candidates) == 1:
            movie_id = candidates[0]["id"]
            _update(movie_id)
        else:
            movie_id = _insert(needs_review=1 if candidates else 0)
    else:
        movie_id = _insert()

    _commit_with_retry(conn)
    conn.close()
    return movie_id


def _movie_filter_clause(search: str | None, category_id: int | None, provider_id: int | None = None) -> tuple[str, list]:
    where = []
    params: list = []
    if search:
        where.append("m.name LIKE ?")
        params.append(f"%{search}%")
    if category_id is not None:
        where.append("m.id IN (SELECT movie_id FROM movie_category_placements WHERE category_id=?)")
        params.append(category_id)
    if provider_id is not None:
        where.append("m.id IN (SELECT movie_id FROM movie_sources WHERE provider_id=?)")
        params.append(provider_id)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    return clause, params


def list_movies(
    limit: int = 50, offset: int = 0, search: str | None = None, category_id: int | None = None,
    provider_id: int | None = None,
) -> list[dict]:
    conn = _connect()
    clause, params = _movie_filter_clause(search, category_id, provider_id)
    rows = conn.execute(
        f"SELECT m.* FROM movies m {clause} ORDER BY m.name LIMIT ? OFFSET ?",
        (*params, limit, offset),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def count_movies(search: str | None = None, category_id: int | None = None, provider_id: int | None = None) -> int:
    conn = _connect()
    clause, params = _movie_filter_clause(search, category_id, provider_id)
    n = conn.execute(f"SELECT COUNT(*) c FROM movies m {clause}", params).fetchone()["c"]
    conn.close()
    return n


def list_all_movie_ids(search: str | None = None, category_id: int | None = None, provider_id: int | None = None) -> list[int]:
    conn = _connect()
    clause, params = _movie_filter_clause(search, category_id, provider_id)
    rows = conn.execute(f"SELECT m.id FROM movies m {clause}", params).fetchall()
    conn.close()
    return [r["id"] for r in rows]


def list_movie_sources_for_ids(movie_ids: list[int]) -> dict[int, list[dict]]:
    """Bulk equivalent of list_movie_sources — one query for a whole page of
    movies instead of one query per movie (that N+1 pattern is what froze the
    app once the pool had thousands of real rows)."""
    if not movie_ids:
        return {}
    conn = _connect()
    placeholders = ",".join("?" for _ in movie_ids)
    rows = conn.execute(f"""
        SELECT ms.*, p.name AS provider_name FROM movie_sources ms
        JOIN providers p ON p.id = ms.provider_id
        WHERE ms.movie_id IN ({placeholders})
        ORDER BY p.name
    """, movie_ids).fetchall()
    conn.close()
    grouped: dict[int, list[dict]] = {mid: [] for mid in movie_ids}
    for r in rows:
        grouped[r["movie_id"]].append(dict(r))
    return grouped


def list_movie_placements_for_ids(movie_ids: list[int]) -> dict[int, list[dict]]:
    if not movie_ids:
        return {}
    conn = _connect()
    placeholders = ",".join("?" for _ in movie_ids)
    rows = conn.execute(f"""
        SELECT mcp.*, c.name AS category_name FROM movie_category_placements mcp
        JOIN categories c ON c.id = mcp.category_id
        WHERE mcp.movie_id IN ({placeholders})
        ORDER BY mcp.id
    """, movie_ids).fetchall()
    conn.close()
    grouped: dict[int, list[dict]] = {mid: [] for mid in movie_ids}
    for r in rows:
        grouped[r["movie_id"]].append(dict(r))
    return grouped


def get_movie(movie_id: int) -> dict | None:
    conn = _connect()
    row = conn.execute("SELECT * FROM movies WHERE id=?", (movie_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_movie_by_name_year(name: str, year: int | None) -> dict | None:
    conn = _connect()
    row = conn.execute("SELECT * FROM movies WHERE name=? AND year IS ?", (name, year)).fetchone()
    conn.close()
    return dict(row) if row else None


_refresh_settings_cache: dict | None = None
_refresh_settings_cache_at = 0.0
_REFRESH_SETTINGS_CACHE_TTL = 30


def _refresh_settings() -> dict:
    """Cached read of config.get_refresh_settings() -- _is_stale() runs once
    per item during a bulk_enrich_all pass over the whole pool (hundreds of
    thousands of rows), so this can't be a raw config-file read per call."""
    global _refresh_settings_cache, _refresh_settings_cache_at
    now = time.time()
    if _refresh_settings_cache is None or (now - _refresh_settings_cache_at) >= _REFRESH_SETTINGS_CACHE_TTL:
        _refresh_settings_cache = get_refresh_settings()
        _refresh_settings_cache_at = now
    return _refresh_settings_cache


def get_enrichment_ttl_seconds() -> int:
    return _refresh_settings()["enrichment_ttl_seconds"]


def get_catalog_refresh_interval_seconds(provider_type: str) -> int:
    key = f"catalog_refresh_seconds_{provider_type}" if provider_type in ("xc", "plex", "emby", "jellyfin") else "catalog_refresh_seconds_xc"
    return _refresh_settings()[key]


def get_tmdb_sync_interval_seconds() -> int | None:
    return _refresh_settings()["tmdb_sync_interval_seconds"]


def mark_provider_catalog_refreshed(provider_id: int) -> None:
    conn = _connect()
    conn.execute("UPDATE providers SET last_catalog_refresh_at=? WHERE id=?", (_now(), provider_id))
    _commit_with_retry(conn)
    conn.close()


def _is_stale(last_enriched_at) -> bool:
    if not last_enriched_at:
        return True
    return (time.time() - float(last_enriched_at)) > get_enrichment_ttl_seconds()


def movie_needs_enrichment(movie_id: int) -> bool:
    movie = get_movie(movie_id)
    return bool(movie) and _is_stale(movie.get("last_enriched_at"))


def set_movie_enrichment(movie_id: int, **fields) -> None:
    """Persist detail-level fields fetched from a provider's get_vod_info, and
    stamp last_enriched_at so we don't re-fetch this movie again for
    ENRICHMENT_TTL_SECONDS — same throttling pattern Dispatcharr itself uses
    for provider detail lookups."""
    conn = _connect()
    fields["last_enriched_at"] = _now()
    sets = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE movies SET {sets} WHERE id=?", (*fields.values(), movie_id))
    _commit_with_retry(conn)
    conn.close()


def list_movie_sources(movie_id: int) -> list[dict]:
    conn = _connect()
    rows = conn.execute("""
        SELECT ms.*, p.name AS provider_name FROM movie_sources ms
        JOIN providers p ON p.id = ms.provider_id
        WHERE ms.movie_id = ?
        ORDER BY p.name
    """, (movie_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def list_movie_placements(movie_id: int) -> list[dict]:
    conn = _connect()
    rows = conn.execute("""
        SELECT mcp.*, c.name AS category_name FROM movie_category_placements mcp
        JOIN categories c ON c.id = mcp.category_id
        WHERE mcp.movie_id = ?
        ORDER BY mcp.id
    """, (movie_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def add_movie_source(
    movie_id: int, provider_id: int, provider_stream_id: str,
    container_extension: str = "mp4", provider_category_name: str | None = None,
) -> None:
    conn = _connect()
    conn.execute(
        """INSERT INTO movie_sources (movie_id, provider_id, provider_stream_id, container_extension, provider_category_name, added_at, last_seen_at)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(provider_id, provider_stream_id) DO UPDATE SET
               movie_id=excluded.movie_id, last_seen_at=excluded.last_seen_at, provider_category_name=excluded.provider_category_name""",
        (movie_id, provider_id, provider_stream_id, container_extension, provider_category_name, _now(), _now()),
    )
    _commit_with_retry(conn)
    conn.close()


def delete_movie(movie_id: int) -> None:
    """Hard delete. movie_sources/movie_category_placements cascade via FK."""
    conn = _connect()
    conn.execute("DELETE FROM movies WHERE id=?", (movie_id,))
    _commit_with_retry(conn)
    conn.close()


def set_movie_adult(movie_id: int, is_adult: bool) -> None:
    """Manual override — also stamps is_adult_manual so future auto-detection
    passes (see resync_adult_flags) never silently revert this."""
    conn = _connect()
    conn.execute(
        "UPDATE movies SET is_adult=?, is_adult_manual=1, updated_at=? WHERE id=?",
        (int(is_adult), _now(), movie_id),
    )
    _commit_with_retry(conn)
    conn.close()


def delete_movie_source(movie_id: int, source_id: int) -> None:
    conn = _connect()
    conn.execute("DELETE FROM movie_sources WHERE id=? AND movie_id=?", (source_id, movie_id))
    _purge_if_sourceless_movie(conn, movie_id)
    _commit_with_retry(conn)
    conn.close()


def remove_movie_from_category(movie_id: int, category_id: int) -> None:
    conn = _connect()
    conn.execute(
        "DELETE FROM movie_category_placements WHERE movie_id=? AND category_id=?",
        (movie_id, category_id),
    )
    _commit_with_retry(conn)
    conn.close()


def place_movie_in_category(movie_id: int, category_id: int) -> int:
    """Assign a movie to a category, returning the export_stream_id to use in the XC feed.

    The first placement for a given movie uses the clean name; subsequent
    placements (for the same movie in additional categories) get an
    invisible zero-width-space marker appended so Dispatcharr's same-account
    (name, year) dedup treats each as a distinct catalog entry.
    """
    conn = _connect()
    flagged = conn.execute("SELECT needs_year_review FROM movies WHERE id=?", (movie_id,)).fetchone()
    if flagged and flagged["needs_year_review"]:
        conn.close()
        raise ValueError(f"movie {movie_id} needs year review before it can be placed in a category")
    existing = conn.execute(
        "SELECT export_stream_id FROM movie_category_placements WHERE movie_id=? AND category_id=?",
        (movie_id, category_id),
    ).fetchone()
    if existing:
        conn.close()
        return existing["export_stream_id"]

    placement_count = conn.execute(
        "SELECT COUNT(*) c FROM movie_category_placements WHERE movie_id=?", (movie_id,)
    ).fetchone()["c"]
    name_suffix = _ZW_MARKER * placement_count  # 0 suffixes for the 1st placement, 1 for the 2nd, ...

    export_stream_id = _EXPORT_STREAM_ID_BASE + _next_placement_seq(conn)
    conn.execute(
        "INSERT INTO movie_category_placements (movie_id, category_id, export_stream_id, name_suffix) VALUES (?,?,?,?)",
        (movie_id, category_id, export_stream_id, name_suffix),
    )
    _commit_with_retry(conn)
    conn.close()
    return export_stream_id


def _next_placement_seq(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COALESCE(MAX(export_stream_id), ?) m FROM movie_category_placements", (_EXPORT_STREAM_ID_BASE - 1,)).fetchone()
    return row["m"] - _EXPORT_STREAM_ID_BASE + 1


def bulk_place_movies_in_category(movie_ids: list[int], category_id: int) -> int:
    """Batch equivalent of place_movie_in_category — one connection/transaction
    for the whole list instead of one round-trip per movie. Needed because
    smart-category evaluation (e.g. a catch-all rule matching the entire
    pool) can place tens of thousands of rows at once; the one-at-a-time
    version times out at that scale. Returns the count newly placed
    (already-placed movies are skipped, same semantics as the single version)."""
    if not movie_ids:
        return 0
    conn = _connect()
    placeholders = ",".join("?" for _ in movie_ids)
    already = {r["movie_id"] for r in conn.execute(
        f"SELECT movie_id FROM movie_category_placements WHERE category_id=? AND movie_id IN ({placeholders})",
        (category_id, *movie_ids),
    ).fetchall()}
    flagged = {r["id"] for r in conn.execute(
        f"SELECT id FROM movies WHERE needs_year_review=1 AND id IN ({placeholders})", movie_ids,
    ).fetchall()}
    if flagged:
        logger.info("[vod_db] skipping %d movie(s) still needing year review for category=%s", len(flagged), category_id)
    to_place = [mid for mid in movie_ids if mid not in already and mid not in flagged]
    if not to_place:
        conn.close()
        return 0

    counts: dict[int, int] = {}
    for r in conn.execute(
        f"SELECT movie_id, COUNT(*) c FROM movie_category_placements WHERE movie_id IN ({','.join('?' for _ in to_place)}) GROUP BY movie_id",
        to_place,
    ).fetchall():
        counts[r["movie_id"]] = r["c"]

    next_seq = _next_placement_seq(conn)
    rows = []
    for mid in to_place:
        name_suffix = _ZW_MARKER * counts.get(mid, 0)
        rows.append((mid, category_id, _EXPORT_STREAM_ID_BASE + next_seq, name_suffix))
        next_seq += 1

    conn.executemany(
        "INSERT INTO movie_category_placements (movie_id, category_id, export_stream_id, name_suffix) VALUES (?,?,?,?)",
        rows,
    )
    _commit_with_retry(conn)
    conn.close()
    return len(rows)


_BEST_SOURCE_CTE = """
    WITH best_source AS (
        SELECT ms.*, ROW_NUMBER() OVER (
            PARTITION BY movie_id ORDER BY pr.priority DESC, last_seen_at DESC
        ) AS rn
        FROM movie_sources ms
        JOIN providers pr ON pr.id = ms.provider_id
        WHERE pr.is_active = 1
    )
"""


def get_movie_export_rows() -> list[dict]:
    """One row per (movie, category placement) for the XC get_vod_streams export.

    Where a movie has sources from multiple providers, the highest-priority
    provider's source is used (recency as tiebreak) — see
    list_movie_sources_for_streaming for the full failover-ordered list.
    """
    conn = _connect()
    rows = conn.execute(_BEST_SOURCE_CTE + """
        SELECT
            m.id AS movie_id, m.name AS name, m.year AS year, m.genre AS genre,
            m.description AS description, m.duration_secs AS duration_secs, m.poster_url AS poster_url,
            m.cast_list AS cast_list, m.director AS director,
            p.export_stream_id AS export_stream_id, p.name_suffix AS name_suffix,
            c.id AS category_id, c.name AS category_name,
            ms.provider_id AS provider_id, ms.provider_stream_id AS provider_stream_id,
            ms.container_extension AS container_extension
        FROM movie_category_placements p
        JOIN movies m ON m.id = p.movie_id
        JOIN categories c ON c.id = p.category_id
        LEFT JOIN best_source ms ON ms.movie_id = m.id AND ms.rn = 1
        ORDER BY m.name
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_movie_source_for_streaming(source_id: int) -> dict | None:
    """One specific movie_sources row, ready for _proxy_vod_stream — used by
    the per-source preview/play button, which forces exactly this provider's
    copy rather than the normal priority-order failover across all of them.
    Includes the parent movie's name/year/duration so the caller can build a
    title without a second lookup."""
    conn = _connect()
    row = conn.execute("""
        SELECT ms.provider_id, ms.provider_stream_id, ms.container_extension, ms.plex_rating_key,
               m.id AS movie_id, m.name AS movie_name, m.year AS movie_year, m.duration_secs AS duration_secs
        FROM movie_sources ms
        JOIN providers p ON p.id = ms.provider_id
        JOIN movies m ON m.id = ms.movie_id
        WHERE ms.id = ? AND p.is_active = 1
    """, (source_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def list_movie_sources_for_streaming(movie_id: int) -> list[dict]:
    """All active-provider sources for a movie, highest-priority-provider
    first (recency as tiebreak) — used by xc_server's stream proxy to fail
    over to another provider if the primary one is down. Unlike
    _BEST_SOURCE_CTE (metadata: one row only), this returns every candidate
    so the proxy can try them in order."""
    conn = _connect()
    rows = conn.execute("""
        SELECT ms.provider_id, ms.provider_stream_id, ms.container_extension, ms.plex_rating_key
        FROM movie_sources ms
        JOIN providers p ON p.id = ms.provider_id
        WHERE ms.movie_id = ? AND p.is_active = 1
        ORDER BY p.priority DESC, ms.last_seen_at DESC
    """, (movie_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_movie_export_row_by_stream_id(export_stream_id: int) -> dict | None:
    conn = _connect()
    row = conn.execute(_BEST_SOURCE_CTE + """
        SELECT
            m.id AS movie_id, m.name AS name, m.year AS year, m.genre AS genre,
            m.description AS description, m.duration_secs AS duration_secs, m.poster_url AS poster_url,
            m.cast_list AS cast_list, m.director AS director,
            p.export_stream_id AS export_stream_id, p.name_suffix AS name_suffix,
            c.id AS category_id, c.name AS category_name,
            ms.provider_id AS provider_id, ms.provider_stream_id AS provider_stream_id,
            ms.container_extension AS container_extension
        FROM movie_category_placements p
        JOIN movies m ON m.id = p.movie_id
        JOIN categories c ON c.id = p.category_id
        LEFT JOIN best_source ms ON ms.movie_id = m.id AND ms.rn = 1
        WHERE p.export_stream_id = ?
        LIMIT 1
    """, (export_stream_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


# ── Series / Episodes ────────────────────────────────────────────────────────

def upsert_series(name: str, year: int | None = None, **fields) -> int:
    conn = _connect()

    def _insert(needs_review: int = 0) -> int:
        cols = ["name", "year", "needs_year_review", *fields.keys()]
        vals = [name, year, needs_review, *fields.values()]
        placeholders = ", ".join("?" for _ in cols)
        cur = conn.execute(
            f"INSERT INTO series ({', '.join(cols)}, created_at) VALUES ({placeholders}, ?)",
            (*vals, _now()),
        )
        return cur.lastrowid

    def _update(series_id: int) -> None:
        if fields:
            sets = ", ".join(f"{k}=?" for k in fields)
            conn.execute(f"UPDATE series SET {sets}, updated_at=? WHERE id=?", (*fields.values(), _now(), series_id))

    row = conn.execute("SELECT id FROM series WHERE name = ? AND year IS ?", (name, year)).fetchone()
    if row:
        series_id = row["id"]
        _update(series_id)
    elif year is None:
        # Same reasoning as upsert_movie above.
        candidates = conn.execute("SELECT id FROM series WHERE name = ?", (name,)).fetchall()
        if len(candidates) == 1:
            series_id = candidates[0]["id"]
            _update(series_id)
        else:
            series_id = _insert(needs_review=1 if candidates else 0)
    else:
        series_id = _insert()

    _commit_with_retry(conn)
    conn.close()
    return series_id


def _series_filter_clause(search: str | None, category_id: int | None, provider_id: int | None = None) -> tuple[str, list]:
    where = []
    params: list = []
    if search:
        where.append("s.name LIKE ?")
        params.append(f"%{search}%")
    if category_id is not None:
        where.append("s.id IN (SELECT series_id FROM series_category_placements WHERE category_id=?)")
        params.append(category_id)
    if provider_id is not None:
        # At least one episode actually sourced from this provider — not
        # import_provider_id, which only reflects whoever created the series
        # row and undercounts providers that later merged episodes in.
        where.append("""s.id IN (
            SELECT e.series_id FROM episode_sources es JOIN episodes e ON e.id = es.episode_id
            WHERE es.provider_id=?
        )""")
        params.append(provider_id)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    return clause, params


def list_series(
    limit: int = 50, offset: int = 0, search: str | None = None, category_id: int | None = None,
    provider_id: int | None = None,
) -> list[dict]:
    conn = _connect()
    clause, params = _series_filter_clause(search, category_id, provider_id)
    rows = conn.execute(
        f"""SELECT s.*, p.name AS import_provider_name FROM series s
            LEFT JOIN providers p ON p.id = s.import_provider_id
            {clause} ORDER BY s.name LIMIT ? OFFSET ?""",
        (*params, limit, offset),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def count_series(search: str | None = None, category_id: int | None = None, provider_id: int | None = None) -> int:
    conn = _connect()
    clause, params = _series_filter_clause(search, category_id, provider_id)
    n = conn.execute(f"SELECT COUNT(*) c FROM series s {clause}", params).fetchone()["c"]
    conn.close()
    return n


def list_all_series_ids(search: str | None = None, category_id: int | None = None, provider_id: int | None = None) -> list[int]:
    conn = _connect()
    clause, params = _series_filter_clause(search, category_id, provider_id)
    rows = conn.execute(f"SELECT s.id FROM series s {clause}", params).fetchall()
    conn.close()
    return [r["id"] for r in rows]


def list_series_placements_for_ids(series_ids: list[int]) -> dict[int, list[dict]]:
    if not series_ids:
        return {}
    conn = _connect()
    placeholders = ",".join("?" for _ in series_ids)
    rows = conn.execute(f"""
        SELECT scp.*, c.name AS category_name FROM series_category_placements scp
        JOIN categories c ON c.id = scp.category_id
        WHERE scp.series_id IN ({placeholders})
        ORDER BY scp.id
    """, series_ids).fetchall()
    conn.close()
    grouped: dict[int, list[dict]] = {sid: [] for sid in series_ids}
    for r in rows:
        grouped[r["series_id"]].append(dict(r))
    return grouped


def episode_export_id(episode_id: int) -> int:
    return _EPISODE_EXPORT_BASE + episode_id


def list_episodes_for_series_ids(series_ids: list[int]) -> dict[int, list[dict]]:
    if not series_ids:
        return {}
    conn = _connect()
    placeholders = ",".join("?" for _ in series_ids)
    rows = conn.execute(
        f"SELECT * FROM episodes WHERE series_id IN ({placeholders}) ORDER BY season_number, episode_number",
        series_ids,
    ).fetchall()
    conn.close()
    grouped: dict[int, list[dict]] = {sid: [] for sid in series_ids}
    for r in rows:
        d = dict(r)
        d["export_episode_id"] = episode_export_id(d["id"])
        grouped[r["series_id"]].append(d)
    return grouped


def get_series(series_id: int) -> dict | None:
    conn = _connect()
    row = conn.execute(
        """SELECT s.*, p.name AS import_provider_name FROM series s
           LEFT JOIN providers p ON p.id = s.import_provider_id
           WHERE s.id=?""",
        (series_id,),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_series_by_name_year(name: str, year: int | None) -> dict | None:
    conn = _connect()
    row = conn.execute("SELECT * FROM series WHERE name=? AND year IS ?", (name, year)).fetchone()
    conn.close()
    return dict(row) if row else None


def series_needs_enrichment(series_id: int) -> bool:
    series = get_series(series_id)
    return bool(series) and _is_stale(series.get("last_enriched_at"))


def set_series_enrichment(series_id: int, **fields) -> None:
    conn = _connect()
    fields["last_enriched_at"] = _now()
    sets = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE series SET {sets} WHERE id=?", (*fields.values(), series_id))
    _commit_with_retry(conn)
    conn.close()


def get_episode(episode_id: int) -> dict | None:
    conn = _connect()
    row = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def add_episode(series_id: int, season_number: int, episode_number: int, name: str, **fields) -> int:
    conn = _connect()
    row = conn.execute(
        "SELECT id FROM episodes WHERE series_id=? AND season_number=? AND episode_number=?",
        (series_id, season_number, episode_number),
    ).fetchone()
    if row:
        episode_id = row["id"]
        fields["name"] = name
        sets = ", ".join(f"{k}=?" for k in fields)
        conn.execute(f"UPDATE episodes SET {sets}, updated_at=? WHERE id=?", (*fields.values(), _now(), episode_id))
    else:
        cols = ["series_id", "season_number", "episode_number", "name", *fields.keys()]
        vals = [series_id, season_number, episode_number, name, *fields.values()]
        placeholders = ", ".join("?" for _ in cols)
        cur = conn.execute(
            f"INSERT INTO episodes ({', '.join(cols)}, created_at) VALUES ({placeholders}, ?)",
            (*vals, _now()),
        )
        episode_id = cur.lastrowid
    _commit_with_retry(conn)
    conn.close()
    return episode_id


def list_episodes(series_id: int) -> list[dict]:
    conn = _connect()
    rows = conn.execute(
        "SELECT * FROM episodes WHERE series_id=? ORDER BY season_number, episode_number", (series_id,)
    ).fetchall()
    conn.close()
    episodes = [dict(r) for r in rows]
    for e in episodes:
        e["export_episode_id"] = episode_export_id(e["id"])
    return episodes


def list_episode_sources_for_episode_ids(episode_ids: list[int]) -> dict[int, list[dict]]:
    """Bulk equivalent of a single-episode source lookup — mirrors
    list_movie_sources_for_ids, which movies already had and episodes never
    did (there was previously no way to see which provider an episode's
    file actually comes from)."""
    if not episode_ids:
        return {}
    conn = _connect()
    placeholders = ",".join("?" for _ in episode_ids)
    rows = conn.execute(f"""
        SELECT es.*, p.name AS provider_name FROM episode_sources es
        JOIN providers p ON p.id = es.provider_id
        WHERE es.episode_id IN ({placeholders})
        ORDER BY p.name
    """, episode_ids).fetchall()
    conn.close()
    grouped: dict[int, list[dict]] = {eid: [] for eid in episode_ids}
    for r in rows:
        grouped[r["episode_id"]].append(dict(r))
    return grouped


def add_episode_source(episode_id: int, provider_id: int, provider_stream_id: str, container_extension: str = "mp4") -> None:
    conn = _connect()
    conn.execute(
        """INSERT INTO episode_sources (episode_id, provider_id, provider_stream_id, container_extension, added_at, last_seen_at)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(provider_id, provider_stream_id) DO UPDATE SET
               episode_id=excluded.episode_id, last_seen_at=excluded.last_seen_at""",
        (episode_id, provider_id, provider_stream_id, container_extension, _now(), _now()),
    )
    _commit_with_retry(conn)
    conn.close()


def delete_series(series_id: int) -> None:
    """Hard delete. episodes cascade via FK, which in turn cascades
    episode_sources; series_category_placements cascade off series directly."""
    conn = _connect()
    conn.execute("DELETE FROM series WHERE id=?", (series_id,))
    _commit_with_retry(conn)
    conn.close()


def set_series_adult(series_id: int, is_adult: bool) -> None:
    """Manual override — also stamps is_adult_manual so future auto-detection
    passes (see resync_adult_flags) never silently revert this."""
    conn = _connect()
    conn.execute(
        "UPDATE series SET is_adult=?, is_adult_manual=1, updated_at=? WHERE id=?",
        (int(is_adult), _now(), series_id),
    )
    _commit_with_retry(conn)
    conn.close()


def delete_episode_source(episode_id: int, source_id: int) -> None:
    conn = _connect()
    conn.execute("DELETE FROM episode_sources WHERE id=? AND episode_id=?", (source_id, episode_id))
    episode_row = conn.execute("SELECT series_id FROM episodes WHERE id=?", (episode_id,)).fetchone()
    _purge_if_sourceless_episode(conn, episode_id)
    if episode_row:
        _purge_if_sourceless_series(conn, episode_row["series_id"])
    _commit_with_retry(conn)
    conn.close()


def remove_series_from_category(series_id: int, category_id: int) -> None:
    conn = _connect()
    conn.execute(
        "DELETE FROM series_category_placements WHERE series_id=? AND category_id=?",
        (series_id, category_id),
    )
    _commit_with_retry(conn)
    conn.close()


def place_series_in_category(series_id: int, category_id: int) -> int:
    """Same virtual-file mechanism as place_movie_in_category, scoped to series."""
    conn = _connect()
    flagged = conn.execute("SELECT needs_year_review FROM series WHERE id=?", (series_id,)).fetchone()
    if flagged and flagged["needs_year_review"]:
        conn.close()
        raise ValueError(f"series {series_id} needs year review before it can be placed in a category")
    existing = conn.execute(
        "SELECT export_series_id FROM series_category_placements WHERE series_id=? AND category_id=?",
        (series_id, category_id),
    ).fetchone()
    if existing:
        conn.close()
        return existing["export_series_id"]

    placement_count = conn.execute(
        "SELECT COUNT(*) c FROM series_category_placements WHERE series_id=?", (series_id,)
    ).fetchone()["c"]
    name_suffix = _ZW_MARKER * placement_count

    row = conn.execute(
        "SELECT COALESCE(MAX(export_series_id), ?) m FROM series_category_placements",
        (_SERIES_EXPORT_BASE - 1,),
    ).fetchone()
    export_series_id = max(row["m"] + 1, _SERIES_EXPORT_BASE)

    conn.execute(
        "INSERT INTO series_category_placements (series_id, category_id, export_series_id, name_suffix) VALUES (?,?,?,?)",
        (series_id, category_id, export_series_id, name_suffix),
    )
    _commit_with_retry(conn)
    conn.close()
    return export_series_id


def bulk_place_series_in_category(series_ids: list[int], category_id: int) -> int:
    """Batch equivalent of place_series_in_category — see bulk_place_movies_in_category."""
    if not series_ids:
        return 0
    conn = _connect()
    placeholders = ",".join("?" for _ in series_ids)
    already = {r["series_id"] for r in conn.execute(
        f"SELECT series_id FROM series_category_placements WHERE category_id=? AND series_id IN ({placeholders})",
        (category_id, *series_ids),
    ).fetchall()}
    flagged = {r["id"] for r in conn.execute(
        f"SELECT id FROM series WHERE needs_year_review=1 AND id IN ({placeholders})", series_ids,
    ).fetchall()}
    if flagged:
        logger.info("[vod_db] skipping %d series still needing year review for category=%s", len(flagged), category_id)
    to_place = [sid for sid in series_ids if sid not in already and sid not in flagged]
    if not to_place:
        conn.close()
        return 0

    counts: dict[int, int] = {}
    for r in conn.execute(
        f"SELECT series_id, COUNT(*) c FROM series_category_placements WHERE series_id IN ({','.join('?' for _ in to_place)}) GROUP BY series_id",
        to_place,
    ).fetchall():
        counts[r["series_id"]] = r["c"]

    row = conn.execute(
        "SELECT COALESCE(MAX(export_series_id), ?) m FROM series_category_placements",
        (_SERIES_EXPORT_BASE - 1,),
    ).fetchone()
    next_id = max(row["m"] + 1, _SERIES_EXPORT_BASE)

    rows = []
    for sid in to_place:
        name_suffix = _ZW_MARKER * counts.get(sid, 0)
        rows.append((sid, category_id, next_id, name_suffix))
        next_id += 1

    conn.executemany(
        "INSERT INTO series_category_placements (series_id, category_id, export_series_id, name_suffix) VALUES (?,?,?,?)",
        rows,
    )
    _commit_with_retry(conn)
    conn.close()
    return len(rows)


def get_series_export_rows() -> list[dict]:
    """One row per (series, category placement) for the XC get_series export."""
    conn = _connect()
    rows = conn.execute("""
        SELECT
            s.id AS series_id, s.name AS name, s.year AS year, s.genre AS genre,
            s.description AS description, s.poster_url AS poster_url,
            s.cast_list AS cast_list, s.director AS director,
            p.export_series_id AS export_series_id, p.name_suffix AS name_suffix,
            c.id AS category_id, c.name AS category_name
        FROM series_category_placements p
        JOIN series s ON s.id = p.series_id
        JOIN categories c ON c.id = p.category_id
        ORDER BY s.name
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_series_export_row_by_export_id(export_series_id: int) -> dict | None:
    conn = _connect()
    row = conn.execute("""
        SELECT
            s.id AS series_id, s.name AS name, s.year AS year, s.genre AS genre,
            s.description AS description, s.poster_url AS poster_url,
            s.cast_list AS cast_list, s.director AS director,
            p.export_series_id AS export_series_id, p.name_suffix AS name_suffix,
            c.id AS category_id, c.name AS category_name
        FROM series_category_placements p
        JOIN series s ON s.id = p.series_id
        JOIN categories c ON c.id = p.category_id
        WHERE p.export_series_id = ?
        LIMIT 1
    """, (export_series_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


_EPISODE_BEST_SOURCE_CTE = """
    WITH best_source AS (
        SELECT es.*, ROW_NUMBER() OVER (
            PARTITION BY episode_id ORDER BY pr.priority DESC, last_seen_at DESC
        ) AS rn
        FROM episode_sources es
        JOIN providers pr ON pr.id = es.provider_id
        WHERE pr.is_active = 1
    )
"""


def get_episode_source_for_streaming(source_id: int) -> dict | None:
    """Episode equivalent of get_movie_source_for_streaming — see there."""
    conn = _connect()
    row = conn.execute("""
        SELECT es.provider_id, es.provider_stream_id, es.container_extension, es.plex_rating_key,
               e.name AS episode_name, e.season_number AS season_number, e.episode_number AS episode_number,
               e.duration_secs AS duration_secs, s.id AS series_id, s.name AS series_name
        FROM episode_sources es
        JOIN providers p ON p.id = es.provider_id
        JOIN episodes e ON e.id = es.episode_id
        JOIN series s ON s.id = e.series_id
        WHERE es.id = ? AND p.is_active = 1
    """, (source_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def list_episode_sources_for_streaming(episode_id: int) -> list[dict]:
    """Episode equivalent of list_movie_sources_for_streaming — see there."""
    conn = _connect()
    rows = conn.execute("""
        SELECT es.provider_id, es.provider_stream_id, es.container_extension, es.plex_rating_key
        FROM episode_sources es
        JOIN providers p ON p.id = es.provider_id
        WHERE es.episode_id = ? AND p.is_active = 1
        ORDER BY p.priority DESC, es.last_seen_at DESC
    """, (episode_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_episode_export_row(episode_id: int) -> dict | None:
    """Episodes don't need the virtual-file dedup trick (only the parent series
    is placed into categories), so the export id is just a stable offset of
    the episode's own row id."""
    conn = _connect()
    row = conn.execute(_EPISODE_BEST_SOURCE_CTE + """
        SELECT
            e.id AS episode_id, e.series_id AS series_id, e.season_number AS season_number,
            e.episode_number AS episode_number, e.name AS name, e.description AS description,
            e.duration_secs AS duration_secs,
            es.provider_id AS provider_id, es.provider_stream_id AS provider_stream_id,
            es.container_extension AS container_extension
        FROM episodes e
        LEFT JOIN best_source es ON es.episode_id = e.id AND es.rn = 1
        WHERE e.id = ?
    """, (episode_id,)).fetchone()
    conn.close()
    if not row:
        return None
    result = dict(row)
    result["export_episode_id"] = _EPISODE_EXPORT_BASE + result["episode_id"]
    return result


def get_episode_export_row_by_export_id(export_episode_id: int) -> dict | None:
    return get_episode_export_row(export_episode_id - _EPISODE_EXPORT_BASE)


def get_episode_export_rows_for_series(series_id: int) -> list[dict]:
    """Bulk equivalent of calling get_episode_export_row once per episode --
    xc_server's get_series_info action used to do exactly that N+1 loop
    (list_episodes, then a separate query per episode), which is fine for a
    short series but opens one SQLite connection per episode synchronously
    inside an async request handler -- for a long-running show (hundreds of
    episodes) that's real blocking time on the single event-loop thread,
    confirmed live: a real Dispatcharr full-catalog sync hitting
    get_series_info for many series in a row froze the whole server for
    every other request until it finished."""
    conn = _connect()
    rows = conn.execute(_EPISODE_BEST_SOURCE_CTE + """
        SELECT
            e.id AS episode_id, e.series_id AS series_id, e.season_number AS season_number,
            e.episode_number AS episode_number, e.name AS name, e.description AS description,
            e.duration_secs AS duration_secs,
            es.provider_id AS provider_id, es.provider_stream_id AS provider_stream_id,
            es.container_extension AS container_extension
        FROM episodes e
        LEFT JOIN best_source es ON es.episode_id = e.id AND es.rn = 1
        WHERE e.series_id = ?
        ORDER BY e.season_number, e.episode_number
    """, (series_id,)).fetchall()
    conn.close()
    results = [dict(r) for r in rows]
    for r in results:
        r["export_episode_id"] = _EPISODE_EXPORT_BASE + r["episode_id"]
    return results


# ── Bulk import ──────────────────────────────────────────────────────────────
# List-level import from a real provider (cheap — name/year/category/stream_id
# only). Runs as a single transaction rather than the usual one-connection-per-
# call pattern, since a real catalog is thousands of rows.

_ADULT_KEYWORDS = ("adult", "xxx", "18+", "porn", "erotic")


def _looks_adult(*category_names) -> bool:
    """Best-effort auto-detect from the provider's own category naming —
    providers almost always segregate adult content into a distinctly-named
    category. Manual overrides (set_movie_adult/set_series_adult) always win;
    this only sets the initial value at creation, never on a later re-import,
    so it doesn't clobber a correction the user already made."""
    for name in category_names:
        if name and any(kw in name.lower() for kw in _ADULT_KEYWORDS):
            return True
    return False


def bulk_import_movies(provider_id: int, items: list[dict]) -> dict:
    """items: [{name, year, provider_stream_id, container_extension, provider_category_name}, ...]

    Adult-content auto-detection runs on every import pass (not just first
    creation) so a provider re-categorizing something later still gets
    picked up on the next scheduled/manual refresh — but only ever upgrades
    is_adult to True from a matching category name, never downgrades, and
    never touches a row a human has manually corrected (is_adult_manual=1).
    """
    conn = _connect()
    now = _now()
    created = 0
    matched = 0
    flagged = 0
    for item in items:
        name = item["name"]
        year = item.get("year")
        category_looks_adult = _looks_adult(item.get("provider_category_name"))
        row = conn.execute("SELECT id, is_adult, is_adult_manual FROM movies WHERE name=? AND year IS ?", (name, year)).fetchone()
        if row:
            movie_id = row["id"]
            matched += 1
            if category_looks_adult and not row["is_adult"] and not row["is_adult_manual"]:
                conn.execute("UPDATE movies SET is_adult=1 WHERE id=?", (movie_id,))
        elif year is None:
            # No exact (name, NULL) row, and no year to key an exact match
            # on -- same reasoning as upsert_movie: exactly one same-named
            # candidate means this is almost certainly it, just missing year
            # metadata from this provider; two or more is genuinely
            # ambiguous, flag rather than silently duplicate.
            candidates = conn.execute("SELECT id FROM movies WHERE name=?", (name,)).fetchall()
            if len(candidates) == 1:
                movie_id = candidates[0]["id"]
                matched += 1
            else:
                cur = conn.execute(
                    "INSERT INTO movies (name, year, is_adult, needs_year_review, created_at) VALUES (?,?,?,?,?)",
                    (name, year, int(category_looks_adult), 1 if candidates else 0, now),
                )
                movie_id = cur.lastrowid
                created += 1
                if candidates:
                    flagged += 1
        else:
            cur = conn.execute(
                "INSERT INTO movies (name, year, is_adult, created_at) VALUES (?,?,?,?)",
                (name, year, int(category_looks_adult), now),
            )
            movie_id = cur.lastrowid
            created += 1
        conn.execute(
            """INSERT INTO movie_sources (movie_id, provider_id, provider_stream_id, container_extension, provider_category_name, added_at, last_seen_at)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(provider_id, provider_stream_id) DO UPDATE SET
                   movie_id=excluded.movie_id, last_seen_at=excluded.last_seen_at, provider_category_name=excluded.provider_category_name""",
            (movie_id, provider_id, item["provider_stream_id"], item.get("container_extension", "mp4"),
             item.get("provider_category_name"), now, now),
        )
    _commit_with_retry(conn)
    conn.close()
    return {"movies_created": created, "movies_matched": matched, "total": len(items), "flagged_for_review": flagged}


def bulk_import_series(provider_id: int, items: list[dict]) -> dict:
    """items: [{name, year, provider_series_id, provider_category_name}, ...]

    Series-level only (XC series don't carry a directly-playable stream_id —
    only their episodes do, which are fetched lazily via get_series_info,
    same as detail enrichment). import_provider_id/import_provider_series_id
    are stamped so enrich_series() can call straight back to the right
    provider instead of re-scanning every provider for a name match."""
    conn = _connect()
    now = _now()
    created = 0
    matched = 0
    flagged = 0
    for item in items:
        name = item["name"]
        year = item.get("year")
        category_looks_adult = _looks_adult(item.get("provider_category_name"))
        row = conn.execute("SELECT id, is_adult, is_adult_manual, import_provider_id FROM series WHERE name=? AND year IS ?", (name, year)).fetchone()
        if row:
            matched += 1
            if category_looks_adult and not row["is_adult"] and not row["is_adult_manual"]:
                conn.execute("UPDATE series SET is_adult=1 WHERE id=?", (row["id"],))
            if row["import_provider_id"] is None:
                # This series previously had no working way to fetch episode
                # detail (e.g. its only prior source's provider was later
                # deleted) -- this provider can, so give it one rather than
                # leaving it permanently stuck.
                conn.execute(
                    "UPDATE series SET import_provider_id=?, import_provider_series_id=? WHERE id=?",
                    (provider_id, item.get("provider_series_id"), row["id"]),
                )
        elif year is None:
            # Same reasoning as bulk_import_movies above.
            candidates = conn.execute("SELECT id, import_provider_id FROM series WHERE name=?", (name,)).fetchall()
            if len(candidates) == 1:
                matched += 1
                if candidates[0]["import_provider_id"] is None:
                    conn.execute(
                        "UPDATE series SET import_provider_id=?, import_provider_series_id=? WHERE id=?",
                        (provider_id, item.get("provider_series_id"), candidates[0]["id"]),
                    )
            else:
                conn.execute(
                    "INSERT INTO series (name, year, is_adult, needs_year_review, import_provider_id, import_provider_series_id, created_at) VALUES (?,?,?,?,?,?,?)",
                    (name, year, int(category_looks_adult), 1 if candidates else 0, provider_id, item.get("provider_series_id"), now),
                )
                created += 1
                if candidates:
                    flagged += 1
        else:
            conn.execute(
                "INSERT INTO series (name, year, is_adult, import_provider_id, import_provider_series_id, created_at) VALUES (?,?,?,?,?,?)",
                (name, year, int(category_looks_adult), provider_id, item.get("provider_series_id"), now),
            )
            created += 1
    _commit_with_retry(conn)
    conn.close()
    return {"series_created": created, "series_matched": matched, "total": len(items), "flagged_for_review": flagged}


_PLEX_DETAIL_FIELDS = ("genre", "description", "director", "cast_list", "poster_url", "last_enriched_at")


def bulk_import_plex_movies(provider_id: int, items: list[dict]) -> dict:
    """Plex counterpart to bulk_import_movies — one connection/transaction for
    the whole library instead of one round-trip per movie (same fix as
    bulk_place_movies_in_category: real-sized libraries time out otherwise).
    Plex hands back full detail up front, so this also writes genre/
    description/etc. in the same pass rather than needing a later enrichment
    step. items: [{name, year, provider_stream_id, container_extension, genre,
    description, director, cast_list, poster_url, last_enriched_at}, ...]"""
    conn = _connect()
    now = _now()
    created = 0
    matched = 0
    flagged = 0
    batch_size = 200
    for i, item in enumerate(items):
        name = item["name"]
        year = item.get("year")
        detail = {k: item.get(k) for k in _PLEX_DETAIL_FIELDS}
        row = conn.execute("SELECT id FROM movies WHERE name=? AND year IS ?", (name, year)).fetchone()
        if row:
            movie_id = row["id"]
            matched += 1
            sets = ", ".join(f"{k}=?" for k in detail)
            conn.execute(f"UPDATE movies SET {sets}, updated_at=? WHERE id=?", (*detail.values(), now, movie_id))
        elif year is None:
            # Same reasoning as bulk_import_movies. Still writes full detail
            # even when flagged -- more info for whoever reviews it later.
            candidates = conn.execute("SELECT id FROM movies WHERE name=?", (name,)).fetchall()
            if len(candidates) == 1:
                movie_id = candidates[0]["id"]
                matched += 1
                sets = ", ".join(f"{k}=?" for k in detail)
                conn.execute(f"UPDATE movies SET {sets}, updated_at=? WHERE id=?", (*detail.values(), now, movie_id))
            else:
                cols = ["name", "year", "needs_year_review", *detail.keys()]
                vals = [name, year, 1 if candidates else 0, *detail.values()]
                placeholders = ", ".join("?" for _ in cols)
                cur = conn.execute(f"INSERT INTO movies ({', '.join(cols)}, created_at) VALUES ({placeholders}, ?)", (*vals, now))
                movie_id = cur.lastrowid
                created += 1
                if candidates:
                    flagged += 1
        else:
            cols = ["name", "year", *detail.keys()]
            vals = [name, year, *detail.values()]
            placeholders = ", ".join("?" for _ in cols)
            cur = conn.execute(f"INSERT INTO movies ({', '.join(cols)}, created_at) VALUES ({placeholders}, ?)", (*vals, now))
            movie_id = cur.lastrowid
            created += 1
        conn.execute(
            """INSERT INTO movie_sources (movie_id, provider_id, provider_stream_id, container_extension, plex_rating_key, added_at, last_seen_at)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(provider_id, provider_stream_id) DO UPDATE SET
                   movie_id=excluded.movie_id, last_seen_at=excluded.last_seen_at, plex_rating_key=excluded.plex_rating_key""",
            (movie_id, provider_id, item["provider_stream_id"], item.get("container_extension", "mp4"), item.get("plex_rating_key"), now, now),
        )
        if (i + 1) % batch_size == 0:
            _commit_with_retry(conn)
    _commit_with_retry(conn)
    conn.close()
    return {"movies_created": created, "movies_matched": matched, "total": len(items), "flagged_for_review": flagged}


def bulk_import_plex_series(provider_id: int, items: list[dict]) -> dict:
    """Plex counterpart to bulk_import_series — same single-transaction fix,
    and also writes every episode (Plex's allLeaves gives us all of them up
    front, unlike XC's lazy per-series fetch) in the same pass. items:
    [{name, year, provider_series_id, genre, description, director,
    cast_list, poster_url, last_enriched_at, episodes: [{season_number,
    episode_number, name, description, duration_secs, provider_stream_id,
    container_extension}, ...]}, ...]"""
    conn = _connect()
    now = _now()
    series_created = 0
    series_matched = 0
    episodes_total = 0
    batch_size = 20
    for i, item in enumerate(items):
        name = item["name"]
        year = item.get("year")
        detail = {k: item.get(k) for k in _PLEX_DETAIL_FIELDS}
        row = conn.execute("SELECT id FROM series WHERE name=? AND year IS ?", (name, year)).fetchone()
        if row:
            series_id = row["id"]
            series_matched += 1
            sets = ", ".join(f"{k}=?" for k in detail)
            conn.execute(f"UPDATE series SET {sets}, updated_at=? WHERE id=?", (*detail.values(), now, series_id))
        else:
            cols = ["name", "year", "import_provider_id", "import_provider_series_id", *detail.keys()]
            vals = [name, year, provider_id, item.get("provider_series_id"), *detail.values()]
            placeholders = ", ".join("?" for _ in cols)
            cur = conn.execute(f"INSERT INTO series ({', '.join(cols)}, created_at) VALUES ({placeholders}, ?)", (*vals, now))
            series_id = cur.lastrowid
            series_created += 1

        for ep in item.get("episodes", []):
            erow = conn.execute(
                "SELECT id FROM episodes WHERE series_id=? AND season_number=? AND episode_number=?",
                (series_id, ep["season_number"], ep["episode_number"]),
            ).fetchone()
            if erow:
                episode_id = erow["id"]
                conn.execute(
                    "UPDATE episodes SET name=?, description=?, duration_secs=?, updated_at=? WHERE id=?",
                    (ep["name"], ep.get("description"), ep.get("duration_secs"), now, episode_id),
                )
            else:
                cur = conn.execute(
                    "INSERT INTO episodes (series_id, season_number, episode_number, name, description, duration_secs, created_at) VALUES (?,?,?,?,?,?,?)",
                    (series_id, ep["season_number"], ep["episode_number"], ep["name"], ep.get("description"), ep.get("duration_secs"), now),
                )
                episode_id = cur.lastrowid
            conn.execute(
                """INSERT INTO episode_sources (episode_id, provider_id, provider_stream_id, container_extension, plex_rating_key, added_at, last_seen_at)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(provider_id, provider_stream_id) DO UPDATE SET
                       episode_id=excluded.episode_id, last_seen_at=excluded.last_seen_at, plex_rating_key=excluded.plex_rating_key""",
                (episode_id, provider_id, ep["provider_stream_id"], ep.get("container_extension", "mp4"), ep.get("plex_rating_key"), now, now),
            )
            episodes_total += 1
        if (i + 1) % batch_size == 0:
            _commit_with_retry(conn)
    _commit_with_retry(conn)
    conn.close()
    return {"series_created": series_created, "series_matched": series_matched, "episodes_imported": episodes_total}


# ── Metadata rewrite rules ───────────────────────────────────────────────────
# Regex find/replace applied to imported title text — e.g. stripping a
# provider's own "4K: " quality-tier prefix so it matches the plain title for
# dedup purposes. Rules are content-agnostic about WHERE they run (import vs.
# enrichment); vod_importer.py calls get_active_rules_for_field at each point
# a given field's value is actually set.

REWRITABLE_FIELDS = ("name", "genre", "description", "director", "cast_list", "country")


def create_metadata_rule(content_type: str, field: str, pattern: str, replacement: str = "", sort_order: int = 0) -> int:
    conn = _connect()
    cur = conn.execute(
        "INSERT INTO metadata_rules (content_type, field, pattern, replacement, sort_order, created_at) VALUES (?,?,?,?,?,?)",
        (content_type, field, pattern, replacement, sort_order, _now()),
    )
    rule_id = cur.lastrowid
    _commit_with_retry(conn)
    conn.close()
    return rule_id


def list_metadata_rules(content_type: str | None = None) -> list[dict]:
    conn = _connect()
    if content_type:
        rows = conn.execute(
            "SELECT * FROM metadata_rules WHERE content_type IN (?, 'both') ORDER BY sort_order, id",
            (content_type,),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM metadata_rules ORDER BY sort_order, id").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def delete_metadata_rule(rule_id: int) -> None:
    conn = _connect()
    conn.execute("DELETE FROM metadata_rules WHERE id=?", (rule_id,))
    _commit_with_retry(conn)
    conn.close()


def set_metadata_rule_active(rule_id: int, is_active: bool) -> None:
    conn = _connect()
    conn.execute("UPDATE metadata_rules SET is_active=? WHERE id=?", (int(is_active), rule_id))
    _commit_with_retry(conn)
    conn.close()


def get_active_rules_for_field(content_type: str, field: str) -> list[dict]:
    conn = _connect()
    rows = conn.execute(
        "SELECT * FROM metadata_rules WHERE content_type IN (?, 'both') AND field=? AND is_active=1 ORDER BY sort_order, id",
        (content_type, field),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def apply_rules_to_value(value: str | None, rules: list[dict]) -> str | None:
    if not value or not rules:
        return value
    import re
    for r in rules:
        try:
            value = re.sub(r["pattern"], r["replacement"], value)
        except re.error as exc:
            logger.warning("[vod_db] bad metadata rule pattern id=%s: %s", r["id"], exc)
    return value


def apply_metadata_rules_to_pool(content_type: str) -> dict:
    """Re-applies all active rules for this content_type against the whole
    already-imported pool — same 'fix what's already there' pattern as the
    year-dedup and enrichment bulk-runs. Movies/series only (episodes don't
    carry independently rewritable text beyond what their parent set)."""
    table = "movies" if content_type == "movie" else "series"
    rules_by_field: dict[str, list[dict]] = {}
    for field in REWRITABLE_FIELDS:
        rules = get_active_rules_for_field(content_type, field)
        if rules:
            rules_by_field[field] = rules
    if not rules_by_field:
        return {"checked": 0, "changed": 0}

    conn = _connect()
    cols = ["id", *rules_by_field.keys()]
    rows = [dict(r) for r in conn.execute(f"SELECT {', '.join(cols)} FROM {table}").fetchall()]

    changed = 0
    for row in rows:
        updates = {}
        for field, rules in rules_by_field.items():
            new_val = apply_rules_to_value(row[field], rules)
            if new_val != row[field]:
                updates[field] = new_val
        if updates:
            sets = ", ".join(f"{f}=?" for f in updates)
            conn.execute(f"UPDATE {table} SET {sets} WHERE id=?", (*updates.values(), row["id"]))
            changed += 1
    _commit_with_retry(conn)
    conn.close()
    return {"checked": len(rows), "changed": changed}


# ── Merging duplicate pool entries ──────────────────────────────────────────
# Used both by the one-time null-year-duplicate cleanup and by the year-review
# resolve flow (a flagged item's year turns out to match an existing entry).
# `into_id`'s own row (name/year/genre/etc.) is left untouched -- it's treated
# as authoritative; `from_id` only ever contributes its sources/episodes/
# placements before being deleted, never overwrites into_id's metadata.

def merge_movie(from_id: int, into_id: int) -> None:
    if from_id == into_id:
        return
    conn = _connect()
    # movie_sources has no per-movie uniqueness (UNIQUE is (provider_id,
    # provider_stream_id) only) -- a plain reassignment can never collide.
    conn.execute("UPDATE movie_sources SET movie_id=? WHERE movie_id=?", (into_id, from_id))

    placements = conn.execute(
        "SELECT category_id FROM movie_category_placements WHERE movie_id=?", (from_id,)
    ).fetchall()
    for p in placements:
        target_has_it = conn.execute(
            "SELECT 1 FROM movie_category_placements WHERE movie_id=? AND category_id=?",
            (into_id, p["category_id"]),
        ).fetchone()
        if target_has_it:
            conn.execute(
                "DELETE FROM movie_category_placements WHERE movie_id=? AND category_id=?",
                (from_id, p["category_id"]),
            )
        else:
            conn.execute(
                "UPDATE movie_category_placements SET movie_id=? WHERE movie_id=? AND category_id=?",
                (into_id, from_id, p["category_id"]),
            )

    conn.execute("DELETE FROM movies WHERE id=?", (from_id,))
    _commit_with_retry(conn)
    conn.close()


def merge_series(from_id: int, into_id: int) -> None:
    if from_id == into_id:
        return
    conn = _connect()

    from_episodes = conn.execute(
        "SELECT id, season_number, episode_number FROM episodes WHERE series_id=?", (from_id,)
    ).fetchall()
    for ep in from_episodes:
        target_ep = conn.execute(
            "SELECT id FROM episodes WHERE series_id=? AND season_number=? AND episode_number=?",
            (into_id, ep["season_number"], ep["episode_number"]),
        ).fetchone()
        if target_ep:
            # Both sides already have this episode -- move from's sources
            # onto into's existing episode row, then drop from's now-empty
            # episode (episode_sources cascades on the episodes delete).
            conn.execute(
                "UPDATE episode_sources SET episode_id=? WHERE episode_id=?",
                (target_ep["id"], ep["id"]),
            )
            conn.execute("DELETE FROM episodes WHERE id=?", (ep["id"],))
        else:
            # into doesn't have this episode yet -- just move it over wholesale.
            conn.execute("UPDATE episodes SET series_id=? WHERE id=?", (into_id, ep["id"]))

    placements = conn.execute(
        "SELECT category_id FROM series_category_placements WHERE series_id=?", (from_id,)
    ).fetchall()
    for p in placements:
        target_has_it = conn.execute(
            "SELECT 1 FROM series_category_placements WHERE series_id=? AND category_id=?",
            (into_id, p["category_id"]),
        ).fetchone()
        if target_has_it:
            conn.execute(
                "DELETE FROM series_category_placements WHERE series_id=? AND category_id=?",
                (from_id, p["category_id"]),
            )
        else:
            conn.execute(
                "UPDATE series_category_placements SET series_id=? WHERE series_id=? AND category_id=?",
                (into_id, from_id, p["category_id"]),
            )

    conn.execute("DELETE FROM series WHERE id=?", (from_id,))
    _commit_with_retry(conn)
    conn.close()


def list_needs_year_review(content_type: str | None = None) -> dict:
    conn = _connect()
    out: dict = {}
    if content_type in (None, "movie"):
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM movies WHERE needs_year_review=1 ORDER BY name"
        ).fetchall()]
        for row in rows:
            # Transcoded fallback preview needs a specific movie_sources row
            # (that route is keyed by source, not movie -- see xc_server.py's
            # /preview/movie-source-transcoded/), for files whose codec the
            # browser can't decode natively (common for Plex-sourced .avi).
            src = conn.execute(
                "SELECT id FROM movie_sources WHERE movie_id=? LIMIT 1", (row["id"],),
            ).fetchone()
            row["sample_source_id"] = src["id"] if src else None
        out["movies"] = rows
    if content_type in (None, "series"):
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM series WHERE needs_year_review=1 ORDER BY name"
        ).fetchall()]
        for row in rows:
            # A preview needs a specific episode (the XC preview route is keyed
            # by episode, not series -- see xc_server.py) so a reviewer can
            # actually watch a clip of the flagged item, not just read its name.
            ep = conn.execute(
                "SELECT id FROM episodes WHERE series_id=? ORDER BY season_number, episode_number LIMIT 1",
                (row["id"],),
            ).fetchone()
            row["sample_episode_id"] = ep["id"] if ep else None
            if ep:
                src = conn.execute(
                    "SELECT id FROM episode_sources WHERE episode_id=? LIMIT 1", (ep["id"],),
                ).fetchone()
                row["sample_episode_source_id"] = src["id"] if src else None
            else:
                row["sample_episode_source_id"] = None
            # Season/episode counts as a secondary signal alongside TMDB
            # suggestions -- if we've pulled 5 seasons/62 episodes and a
            # candidate's own TMDB counts are wildly different, that's a
            # useful hint even when the name/year alone are ambiguous. Not
            # every provider has a complete catalog, so this is corroborating
            # evidence, not proof either way.
            counts = conn.execute(
                "SELECT COUNT(DISTINCT season_number) seasons, COUNT(*) episodes FROM episodes WHERE series_id=?",
                (row["id"],),
            ).fetchone()
            row["imported_season_count"] = counts["seasons"]
            row["imported_episode_count"] = counts["episodes"]
        out["series"] = rows
    conn.close()
    return out


def resolve_year_review(content_type: str, item_id: int, year: int, tmdb_id: str | None = None) -> dict:
    """Sets the correct year (and tmdb_id, if known) on a flagged item and
    clears the flag. If that year now exactly matches an existing item of
    the same name, merges into it instead of leaving two rows around."""
    table = "movies" if content_type == "movie" else "series"
    conn = _connect()
    row = conn.execute(f"SELECT * FROM {table} WHERE id=?", (item_id,)).fetchone()
    if not row:
        conn.close()
        raise ValueError(f"{content_type} {item_id} not found")

    existing = conn.execute(
        f"SELECT id FROM {table} WHERE name=? AND year=? AND id != ?", (row["name"], year, item_id),
    ).fetchone()
    conn.close()

    if existing:
        if content_type == "movie":
            merge_movie(item_id, existing["id"])
        else:
            merge_series(item_id, existing["id"])
        return {"merged_into": existing["id"]}

    conn = _connect()
    fields = {"year": year, "needs_year_review": 0}
    if tmdb_id:
        fields["tmdb_id"] = tmdb_id
    sets = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE {table} SET {sets}, updated_at=? WHERE id=?", (*fields.values(), _now(), item_id))
    _commit_with_retry(conn)
    conn.close()
    return {"resolved_id": item_id}


# ── Missing artwork ──────────────────────────────────────────────────────────
# Poster/tmdb_id normally come straight from the XC provider's own metadata
# (see vod_importer.enrich_movie/enrich_series) -- when a provider's catalog
# just doesn't have artwork for something (or its title is mangled enough
# that the provider's own match failed), this is the browse-and-fix queue: a
# real TMDB search (tmdb_sync.search_title) a human or the AI picks from,
# same review-before-apply shape as needs_year_review above.

def list_missing_artwork(content_type: str, limit: int = 50, offset: int = 0, search: str | None = None) -> list[dict]:
    table = "movies" if content_type == "movie" else "series"
    conn = _connect()
    where = ["(poster_url IS NULL OR poster_url = '')"]
    params: list = []
    if search:
        where.append("name LIKE ?")
        params.append(f"%{search}%")
    clause = f"WHERE {' AND '.join(where)}"
    rows = conn.execute(
        f"SELECT * FROM {table} {clause} ORDER BY name LIMIT ? OFFSET ?",
        (*params, limit, offset),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def count_missing_artwork(content_type: str, search: str | None = None) -> int:
    table = "movies" if content_type == "movie" else "series"
    conn = _connect()
    where = ["(poster_url IS NULL OR poster_url = '')"]
    params: list = []
    if search:
        where.append("name LIKE ?")
        params.append(f"%{search}%")
    clause = f"WHERE {' AND '.join(where)}"
    n = conn.execute(f"SELECT COUNT(*) c FROM {table} {clause}", params).fetchone()["c"]
    conn.close()
    return n


def resolve_missing_artwork(
    content_type: str, item_id: int, poster_url: str,
    tmdb_id: str | None = None, name: str | None = None, year: int | None = None,
) -> dict:
    """Applies a chosen TMDB match (or a manually-entered poster URL) to a
    missing-artwork item. name/year are optional -- a corrected search query
    often reveals the *stored* name was the actual problem, so a reviewer or
    the AI can fix that at the same time, not just the poster. Same
    merge-on-collision safety as resolve_year_review: if the corrected
    name/year now matches an existing pool entry exactly, merge into it
    rather than leaving two rows with the same identity."""
    table = "movies" if content_type == "movie" else "series"
    conn = _connect()
    row = conn.execute(f"SELECT * FROM {table} WHERE id=?", (item_id,)).fetchone()
    if not row:
        conn.close()
        raise ValueError(f"{content_type} {item_id} not found")

    final_name = name.strip() if name and name.strip() else row["name"]
    final_year = year if year is not None else row["year"]

    existing = None
    if (final_name, final_year) != (row["name"], row["year"]):
        existing = conn.execute(
            f"SELECT id FROM {table} WHERE name=? AND year IS ? AND id != ?", (final_name, final_year, item_id),
        ).fetchone()

    if existing:
        # Give the surviving row the poster/tmdb_id before folding this one
        # into it, in case it was ALSO missing artwork.
        conn.execute(
            f"UPDATE {table} SET poster_url=COALESCE(NULLIF(poster_url,''), ?), "
            f"tmdb_id=COALESCE(tmdb_id, ?), updated_at=? WHERE id=?",
            (poster_url, tmdb_id, _now(), existing["id"]),
        )
        _commit_with_retry(conn)
        conn.close()
        if content_type == "movie":
            merge_movie(item_id, existing["id"])
        else:
            merge_series(item_id, existing["id"])
        return {"merged_into": existing["id"]}

    fields = {"poster_url": poster_url, "name": final_name, "year": final_year}
    if tmdb_id:
        fields["tmdb_id"] = tmdb_id
    sets = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE {table} SET {sets}, updated_at=? WHERE id=?", (*fields.values(), _now(), item_id))
    _commit_with_retry(conn)
    conn.close()
    return {"resolved_id": item_id}


# ── Smart categories ─────────────────────────────────────────────────────────
# rule_json shape: {"match": "all"|"any", "conditions": [{"field", "op", "value"}, ...]}
# field: name | genre | year | country | director (movies/series share these)
# op: contains | equals | starts_with | gte | lte

_SMART_CATEGORY_FIELDS = {"name", "genre", "year", "country", "language", "director", "is_adult"}
# "language" isn't a real column — providers report spoken language(s) in what
# we store as "country" (e.g. "English, Español"), so it's an alias onto that
# same data rather than a separate field. Named clearly for the UI since
# "country" reads as country-of-origin, not language.
_FIELD_ALIASES = {"language": "country"}


def _to_num(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _condition_matches(row: dict, cond: dict) -> bool:
    field = cond.get("field")
    op    = cond.get("op")
    value = cond.get("value")
    if field not in _SMART_CATEGORY_FIELDS:
        return False
    actual = row.get(_FIELD_ALIASES.get(field, field))

    if op == "contains":
        return str(value or "").strip().lower() in str(actual or "").lower()
    if op == "starts_with":
        return str(actual or "").lower().startswith(str(value or "").strip().lower())
    if op == "equals":
        return str(actual if actual is not None else "").strip().lower() == str(value or "").strip().lower()
    if op in ("gte", "lte"):
        a, v = _to_num(actual), _to_num(value)
        if a is None or v is None:
            return False
        return a >= v if op == "gte" else a <= v
    return False


def _rule_matches(row: dict, rule: dict) -> bool:
    conditions = rule.get("conditions") or []
    if not conditions:
        return False
    checks = (_condition_matches(row, c) for c in conditions)
    return all(checks) if rule.get("match", "all") == "all" else any(checks)


def evaluate_smart_category(category_id: int) -> dict:
    """Evaluate a smart category's rule_json against the whole pool (movies or
    series, per the category's content_type) and auto-place every match.
    Never un-places existing matches — same additive semantics as manual
    placement. Returns counts for the caller to surface in the UI."""
    category = get_category(category_id)
    if not category:
        raise ValueError(f"category {category_id} not found")
    if not category["is_smart"]:
        raise ValueError(f"category {category_id} is not a smart category")
    if not category["rule_json"]:
        raise ValueError(f"category {category_id} has no rule_json configured")

    import json
    rule = json.loads(category["rule_json"])

    conn = _connect()
    if category["content_type"] == "movie":
        rows = [dict(r) for r in conn.execute("SELECT * FROM movies").fetchall()]
    else:
        rows = [dict(r) for r in conn.execute("SELECT * FROM series").fetchall()]
    conn.close()

    matched_ids = [row["id"] for row in rows if _rule_matches(row, rule)]
    if category["content_type"] == "movie":
        newly_placed = bulk_place_movies_in_category(matched_ids, category_id)
    else:
        newly_placed = bulk_place_series_in_category(matched_ids, category_id)

    return {"evaluated": len(rows), "matched": len(matched_ids), "newly_placed": newly_placed}


def get_ai_candidate_rows(content_type: str, prefilter_rule_json: str | None, limit: int) -> tuple[list[dict], int]:
    """Bounded candidate pool for AI Evaluate (see ai_assist.py's
    evaluate_candidates_for_category) -- real per-item API cost means this
    can never run over the raw pool. Reuses the exact same rule_json
    pre-filter mechanism as rule-based smart categories (see
    evaluate_smart_category above) to narrow the field before applying the
    cap; without a pre-filter, it's just the first `limit` rows by id.
    Returns (candidates, total_before_cap) so the caller can tell the user
    how much was left out, rather than silently truncating."""
    import json
    conn = _connect()
    if content_type == "movie":
        rows = [dict(r) for r in conn.execute("SELECT * FROM movies").fetchall()]
    else:
        rows = [dict(r) for r in conn.execute("SELECT * FROM series").fetchall()]
    conn.close()

    if prefilter_rule_json:
        rule = json.loads(prefilter_rule_json)
        rows = [r for r in rows if _rule_matches(r, rule)]

    return rows[:limit], len(rows)
