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
import secrets
import sqlite3
import time
from pathlib import Path

from config import DATA_DIR

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

        CREATE INDEX IF NOT EXISTS idx_movies_name_year ON movies(name, year);
        CREATE INDEX IF NOT EXISTS idx_series_name_year ON series(name, year);
        CREATE INDEX IF NOT EXISTS idx_episodes_series_season_ep ON episodes(series_id, season_number, episode_number);
    """)
    _commit_with_retry(conn)
    _migrate(conn)
    conn.close()


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


def set_provider_connection_sharing(
    provider_id: int, dispatcharr_live_account_id: int | None, shared_connection_limit: int | None,
) -> None:
    """Configures cross-system connection coordination — see xc_server.py's
    _has_capacity(). dispatcharr_live_account_id identifies the Dispatcharr
    M3U account (if any) that ALSO connects to this same real provider for
    live TV; shared_connection_limit is the real provider's true total
    connection cap, shared across live TV + our own VOD streaming."""
    conn = _connect()
    conn.execute(
        "UPDATE providers SET dispatcharr_live_account_id=?, shared_connection_limit=?, updated_at=? WHERE id=?",
        (dispatcharr_live_account_id, shared_connection_limit, _now(), provider_id),
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
    conn.close()
    for p in rows:
        p["movie_count"] = movie_counts.get(p["id"], 0)
        p["series_count"] = series_counts.get(p["id"], 0)
        p["episode_count"] = episode_counts.get(p["id"], 0)
    return rows


def set_provider_active(provider_id: int, is_active: bool) -> None:
    conn = _connect()
    conn.execute("UPDATE providers SET is_active=?, updated_at=? WHERE id=?", (int(is_active), _now(), provider_id))
    _commit_with_retry(conn)
    conn.close()


def delete_provider(provider_id: int) -> None:
    """Hard delete. movie_sources/episode_sources for this provider cascade
    via FK (ON DELETE CASCADE) — the movies/series themselves are left intact
    even if this was their only source, same as any other source loss."""
    conn = _connect()
    conn.execute("DELETE FROM providers WHERE id=?", (provider_id,))
    _commit_with_retry(conn)
    conn.close()


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


ENRICHMENT_TTL_SECONDS = 24 * 3600


def _is_stale(last_enriched_at) -> bool:
    if not last_enriched_at:
        return True
    return (time.time() - float(last_enriched_at)) > ENRICHMENT_TTL_SECONDS


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
           ON CONFLICT(provider_id, provider_stream_id) DO UPDATE SET last_seen_at=excluded.last_seen_at, provider_category_name=excluded.provider_category_name""",
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
            m.description AS description, m.duration_secs AS duration_secs,
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
               m.name AS movie_name, m.year AS movie_year, m.duration_secs AS duration_secs
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
            m.description AS description, m.duration_secs AS duration_secs,
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
           ON CONFLICT(provider_id, provider_stream_id) DO UPDATE SET last_seen_at=excluded.last_seen_at""",
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
            s.description AS description,
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
            s.description AS description,
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
               e.duration_secs AS duration_secs, s.name AS series_name
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
                   last_seen_at=excluded.last_seen_at, provider_category_name=excluded.provider_category_name""",
            (movie_id, provider_id, item["provider_stream_id"], item.get("container_extension", "mp4"),
             item.get("provider_category_name"), now, now),
        )
    _commit_with_retry(conn)
    conn.close()
    return {"movies_created": created, "movies_matched": matched, "total": len(items)}


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
    for item in items:
        name = item["name"]
        year = item.get("year")
        category_looks_adult = _looks_adult(item.get("provider_category_name"))
        row = conn.execute("SELECT id, is_adult, is_adult_manual FROM series WHERE name=? AND year IS ?", (name, year)).fetchone()
        if row:
            matched += 1
            if category_looks_adult and not row["is_adult"] and not row["is_adult_manual"]:
                conn.execute("UPDATE series SET is_adult=1 WHERE id=?", (row["id"],))
        else:
            conn.execute(
                "INSERT INTO series (name, year, is_adult, import_provider_id, import_provider_series_id, created_at) VALUES (?,?,?,?,?,?)",
                (name, year, int(category_looks_adult), provider_id, item.get("provider_series_id"), now),
            )
            created += 1
    _commit_with_retry(conn)
    conn.close()
    return {"series_created": created, "series_matched": matched, "total": len(items)}


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
               ON CONFLICT(provider_id, provider_stream_id) DO UPDATE SET last_seen_at=excluded.last_seen_at, plex_rating_key=excluded.plex_rating_key""",
            (movie_id, provider_id, item["provider_stream_id"], item.get("container_extension", "mp4"), item.get("plex_rating_key"), now, now),
        )
        if (i + 1) % batch_size == 0:
            _commit_with_retry(conn)
    _commit_with_retry(conn)
    conn.close()
    return {"movies_created": created, "movies_matched": matched, "total": len(items)}


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
                   ON CONFLICT(provider_id, provider_stream_id) DO UPDATE SET last_seen_at=excluded.last_seen_at, plex_rating_key=excluded.plex_rating_key""",
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
        out["movies"] = [dict(r) for r in conn.execute(
            "SELECT * FROM movies WHERE needs_year_review=1 ORDER BY name"
        ).fetchall()]
    if content_type in (None, "series"):
        out["series"] = [dict(r) for r in conn.execute(
            "SELECT * FROM series WHERE needs_year_review=1 ORDER BY name"
        ).fetchall()]
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
