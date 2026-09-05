"""
Imports a real provider's VOD catalog into our own pool.

Two-phase, matching how Dispatcharr itself (and every XC client) actually
handles this: a cheap bulk list import (name/year/category/stream_id — one
call for the whole catalog) now, and expensive per-item detail enrichment
(genre, cast, tmdb_id, poster, description) fetched lazily on demand and
cached (see vod_db.ENRICHMENT_TTL_SECONDS). bulk_enrich_all() below covers
the whole pool at once, still one item at a time under the hood, just with
bounded concurrency instead of a human clicking one movie at a time.
"""

import asyncio
import logging
import re
import sqlite3
import time

import httpx

import config
import emby_vod_client
import tmdb_sync
import vod_db
from xc_server import _redact_upstream_url


def _should_auto_archive(
    name: str, provider_category_name: str | None = None, provider_exclude_categories: list[str] = (),
    exclude_uncategorized: bool = False,
) -> bool:
    """Import-time equivalent of the manual Language Filter archive tool --
    deliberately NOT sibling-safe (see USERGUIDE's Language Filter section
    for that tool's "don't archive the only copy" behavior): an explicit
    exclusion rule means "I don't want this content in my library at all",
    not "prefer another language's copy if one exists". Language rules are
    global (config.get_import_language_exclusion); category rules are
    per-provider (providers.import_exclude_categories), since available
    categories genuinely differ provider to provider.

    provider_category_name/provider_exclude_categories default to no-ops
    for callers that only want language rules -- plex_importer.py/
    emby_vod_importer.py (GH#9) pass their own provider's library/
    collection-folder name in this same field, since a Plex library
    section (or Emby/Jellyfin virtual folder) is exactly what a user means
    by "category" for those provider types, even though it's not an
    XC-style flat category list under the hood.

    exclude_uncategorized (GH issue #7): a real provider was found shipping
    movies with no category attached at all -- the category-name check below
    can never catch that (there's no name to compare), so this is a
    dedicated switch, checked only when the item truly has no category,
    never as a substitute for an actual category-name match."""
    lang = config.get_import_language_exclusion()
    if lang["exclude_prefixes"]:
        code = vod_db._name_prefix_code(name)
        if code and code in lang["exclude_prefixes"]:
            return True
    if lang["exclude_non_latin"] and vod_db._is_non_latin_name(name):
        return True
    if provider_category_name:
        if provider_category_name in provider_exclude_categories:
            return True
    elif exclude_uncategorized:
        return True
    return False


def _as_dict(value) -> dict:
    """get_vod_info/get_series_info are documented as returning an object,
    but at least one real provider returns a bare list (e.g. `[]`) instead
    of `{}` for "no data" -- either at the top level or nested under
    "info" -- which crashed every .get() downstream with 'list' object has
    no attribute 'get', silently failing that item's whole enrichment (and,
    for series, its episodes -- see enrich_series). Treat anything that
    isn't actually a dict as "no data" instead of raising."""
    return value if isinstance(value, dict) else {}

logger = logging.getLogger(__name__)

_YEAR_SUFFIX_RE = re.compile(
    # (?<!\d) keeps this from firing inside a genuine in-title year range like
    # "... (1987-1997)" or "Wartorn: 1861-2010" -- without it, the dash/paren
    # right before the second year in the range looks identical to a real
    # trailing year suffix and the title gets mangled.
    r"^(.*?)\s*(?<!\d)[-(]\s*(19\d{2}|20\d{2})\)?\s*(?:\[[^\]]*\]|[A-Z][A-Z\- ]{2,})?\s*$"
)

# Some real XC providers silently drop the connection -- no HTTP response
# at all -- for requests without a browser-like User-Agent,
# httpx's default ("python-httpx/x.y.z") included. A generic desktop-browser
# UA is enough to get a normal response.
_UPSTREAM_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"}


def _coerce_int(value) -> int | None:
    """Same reasoning as _coerce_year below, generalized -- bitrate in
    particular has been observed as a plain int from one real provider and
    there's no guarantee another sends it consistently."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _coerce_year(value) -> int | None:
    """Some XC providers send the series "year" field as a string (or an
    empty string, or junk like "N/A") rather than a number -- SQLite's
    INTEGER column affinity happens to silently coerce a clean numeric
    string on insert, which is exactly why this went unnoticed here, but
    anything that doesn't look like a plain year would still get stored
    as-is and quietly break every exact (name, year) match downstream
    (series import's own dedup lookup, needs_year_review, the duplicate
    finder's group scan)."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _clean_tmdb_id(value) -> str | None:
    """Real user data found live 2026-09-05: at least one provider sends
    tmdb="0" as its own "no id known" sentinel rather than omitting the
    field -- a plain `value or None` check treats the non-empty string "0"
    as truthy, so that sentinel was getting stored as a real tmdb_id (149 of
    ~11k series in one real pool). Every tmdb_id extraction site should run
    through this instead of a bare truthiness check."""
    if value in (None, "", 0, "0"):
        return None
    return str(value)


def parse_name_year(raw_name: str) -> tuple[str, int | None]:
    """Real XC providers commonly bake the year into the title string itself,
    not always as a clean trailing "(YYYY)" -- also seen: "Title - YYYY",
    "Title (YYYY) [MULTI-SUB]", "Title (YYYY) HINDI", and even an unclosed
    "Title (YYYY". Some catalogs duplicate it (e.g. "1 1 (2018) (2018)") --
    strip every trailing year layer, not just one, or the leftover copy in
    the name doubles up with the year we display alongside it."""
    name = raw_name.strip()
    year = None
    while True:
        m = _YEAR_SUFFIX_RE.match(name)
        if not m:
            break
        new_name = m.group(1).strip()
        if new_name == name:
            break
        name, year = new_name, int(m.group(2))
    return name, year


class ProviderBackoffError(Exception):
    """Raised in place of an actual network call while a provider is under
    an active backoff cooldown (see _PROVIDER_BACKOFF below) -- lets a
    caller (bulk_enrich_all's per-item loop) skip the item cheaply, with no
    request sent, instead of piling more load onto a provider that's
    already shown signs of rate-limiting/blocking."""


# Per-provider backoff state, keyed by provider_id: {"failures": int,
# "until": float (time.time() timestamp, 0.0 if not currently backing off)}.
# Real user report 2026-09-02: bulk enrich against a large catalog reliably
# started throwing "Temporary failure in name resolution" (DNS-level, i.e.
# the provider or the network path to it stopped responding at all) after a
# sustained burst of requests, then 403s once it came back -- classic
# provider-side throttling/blocking under load, not anything the app was
# doing wrong per item. Bulk enrich has no per-provider rate limit of its
# own (just a flat concurrency=8 semaphore shared across every source), so
# once a provider starts rejecting/dropping connections, every one of those
# 8 concurrent slots kept hammering it in lockstep instead of backing off --
# maximizing exactly the load pattern that trips a provider's own rate
# limiter, and guaranteeing every retry attempt (this run or the next) would
# hit the exact same wall.
_PROVIDER_BACKOFF: dict[int, dict] = {}
_BACKOFF_FAILURE_THRESHOLD = 3
_BACKOFF_BASE_SECONDS = 15.0
_BACKOFF_MAX_SECONDS = 600.0
# Status codes and exception types that indicate the PROVIDER itself is
# throttling/blocking -- as opposed to e.g. a 404 for one missing/removed
# item, which says nothing about the provider's overall health and would
# otherwise trip the same backoff for every other item for no reason.
_BACKOFF_STATUS_CODES = {403, 429, 503}
_BACKOFF_EXCEPTION_TYPES = (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.PoolTimeout)


def _provider_backoff_remaining(provider_id: int) -> float:
    state = _PROVIDER_BACKOFF.get(provider_id)
    if not state:
        return 0.0
    return max(0.0, state["until"] - time.time())


def _record_provider_failure(provider_id: int, provider_name: str) -> None:
    state = _PROVIDER_BACKOFF.setdefault(provider_id, {"failures": 0, "until": 0.0})
    state["failures"] += 1
    if state["failures"] >= _BACKOFF_FAILURE_THRESHOLD:
        # Exponential in how many threshold-lengths of failures have piled
        # up, so a provider that keeps failing through one cooldown gets a
        # longer one next time instead of getting hammered again the moment
        # the short cooldown expires.
        tier = state["failures"] // _BACKOFF_FAILURE_THRESHOLD
        delay = min(_BACKOFF_MAX_SECONDS, _BACKOFF_BASE_SECONDS * (2 ** (tier - 1)))
        already_backing_off = _provider_backoff_remaining(provider_id) > 0
        state["until"] = time.time() + delay
        if not already_backing_off:
            logger.warning(
                "[vod_importer] provider=%s looks rate-limited/blocked (%d consecutive failures) -- "
                "backing off enrichment requests to it for %.0fs",
                provider_name, state["failures"], delay,
            )


def _record_provider_success(provider_id: int) -> None:
    state = _PROVIDER_BACKOFF.get(provider_id)
    if state and (state["failures"] or state["until"]):
        logger.info("[vod_importer] provider=%s recovered -- clearing backoff", provider_id)
    _PROVIDER_BACKOFF.pop(provider_id, None)


# Adaptive per-provider concurrency cap, layered on top of the binary
# backoff above -- raised in a 2026-09-04 discussion prompted by a user still
# seeing real provider rate-limiting: the binary backoff only ever goes from
# "full concurrency" to "fully paused", with no middle ground. This narrows a
# provider's own share of the concurrency budget gradually the moment it
# shows trouble (halves it, floor 1), then creeps back up by 1 after a run of
# clean calls -- an AIMD approach, same shape as TCP congestion control, so a
# genuinely-throttled provider settles near whatever concurrency it can
# actually sustain instead of alternating between "wide open" and "fully
# stopped". A healthy provider is never artificially slowed down -- every
# provider starts at _PROVIDER_MAX_CONCURRENCY (today's unrestricted
# behavior) and only narrows in response to an actual failure signal (the
# same _BACKOFF_STATUS_CODES/_BACKOFF_EXCEPTION_TYPES the binary backoff
# already watches for). In-memory only, same as _PROVIDER_BACKOFF -- resets
# on restart, which just means a provider gets to prove itself healthy again
# rather than staying permanently throttled from a stale prior run. This is
# independent of bulk_enrich_all's own `concurrency` semaphore (which caps
# TOTAL in-flight movie/series work across every provider combined) -- this
# caps one specific provider's share of that budget, which the shared
# semaphore alone can't do when several providers' items are interleaved in
# the same run.
_PROVIDER_MIN_CONCURRENCY = 1
_PROVIDER_MAX_CONCURRENCY = 8
_PROVIDER_RAMP_SUCCESSES = 25  # consecutive clean calls at the current cap before nudging it up by 1


class _AdaptiveLimiter:
    def __init__(self) -> None:
        self.cap = _PROVIDER_MAX_CONCURRENCY
        self._in_use = 0
        self._streak = 0
        self._cond = asyncio.Condition()

    async def acquire(self) -> None:
        async with self._cond:
            while self._in_use >= self.cap:
                await self._cond.wait()
            self._in_use += 1

    async def release(self) -> None:
        async with self._cond:
            self._in_use -= 1
            self._cond.notify_all()

    async def note_failure(self) -> None:
        async with self._cond:
            self._streak = 0
            self.cap = max(_PROVIDER_MIN_CONCURRENCY, self.cap // 2)

    async def note_success(self) -> None:
        async with self._cond:
            if self.cap >= _PROVIDER_MAX_CONCURRENCY:
                self._streak = 0
                return
            self._streak += 1
            if self._streak >= _PROVIDER_RAMP_SUCCESSES:
                self.cap += 1
                self._streak = 0
                self._cond.notify_all()  # newly-available slot(s) -- wake any acquire() waiters


_PROVIDER_LIMITERS: dict[int, _AdaptiveLimiter] = {}


def _get_provider_limiter(provider_id: int) -> _AdaptiveLimiter:
    limiter = _PROVIDER_LIMITERS.get(provider_id)
    if limiter is None:
        limiter = _AdaptiveLimiter()
        _PROVIDER_LIMITERS[provider_id] = limiter
    return limiter


class XCProviderClient:
    def __init__(self, provider: dict):
        self.provider = provider
        self.provider_id = provider.get("id")
        self.provider_name = provider.get("name") or str(self.provider_id)
        self.base_url = provider["base_url"].rstrip("/")
        self.username = provider["username"]
        self.password = provider["password"]
        custom_ua = provider.get("custom_user_agent")
        self.headers = {"User-Agent": custom_ua} if custom_ua else _UPSTREAM_HEADERS

    async def _call(self, action: str | None = None, **params) -> object:
        limiter = None
        if self.provider_id is not None:
            remaining = _provider_backoff_remaining(self.provider_id)
            if remaining > 0:
                raise ProviderBackoffError(
                    f"provider={self.provider_name} is backing off for another {remaining:.0f}s (looked rate-limited/blocked)"
                )
            limiter = _get_provider_limiter(self.provider_id)
            await limiter.acquire()
        query = {"username": self.username, "password": self.password}
        if action:
            query["action"] = action
        query.update(params)
        try:
            try:
                async with httpx.AsyncClient(timeout=30.0, follow_redirects=True, headers=self.headers) as client:
                    r = await client.get(f"{self.base_url}/player_api.php", params=query)
                    r.raise_for_status()
                    result = r.json()
            except _BACKOFF_EXCEPTION_TYPES:
                if self.provider_id is not None:
                    _record_provider_failure(self.provider_id, self.provider_name)
                    await limiter.note_failure()
                raise
            except httpx.HTTPStatusError as exc:
                if self.provider_id is not None and exc.response.status_code in _BACKOFF_STATUS_CODES:
                    _record_provider_failure(self.provider_id, self.provider_name)
                    await limiter.note_failure()
                raise
            else:
                if self.provider_id is not None:
                    _record_provider_success(self.provider_id)
                    await limiter.note_success()
                return result
        finally:
            if limiter is not None:
                await limiter.release()

    async def auth(self) -> dict:
        return await self._call()

    async def get_vod_categories(self) -> list[dict]:
        return await self._call("get_vod_categories")

    async def get_vod_streams(self) -> list[dict]:
        return await self._call("get_vod_streams")

    async def get_vod_info(self, vod_id: str) -> dict:
        return await self._call("get_vod_info", vod_id=vod_id)

    async def get_series_categories(self) -> list[dict]:
        return await self._call("get_series_categories")

    async def get_series(self) -> list[dict]:
        return await self._call("get_series")

    async def get_series_info(self, series_id: str) -> dict:
        return await self._call("get_series_info", series_id=series_id)


async def import_provider_catalog(provider_id: int) -> dict:
    provider = await asyncio.to_thread(vod_db.get_provider, provider_id)
    if not provider:
        raise ValueError(f"provider {provider_id} not found")

    client = XCProviderClient(provider)

    exclude_categories = provider.get("import_exclude_categories") or []
    exclude_uncategorized = bool(provider.get("import_exclude_uncategorized"))

    # Stripped for the same reason vod_routes.get_provider_available_categories
    # strips (GH#4 reopened): provider_exclude_categories is saved trimmed
    # (vod_db.set_provider_import_exclude_categories), so an untrimmed name
    # here would never match its own saved exclusion below -- a category
    # with stray whitespace in its provider-reported name could never
    # actually be excluded, no matter how many times it was re-selected.
    categories = await client.get_vod_categories()
    category_names = {str(c["category_id"]): (c.get("category_name") or "").strip() for c in categories}

    series_categories = await client.get_series_categories()
    series_category_names = {str(c["category_id"]): (c.get("category_name") or "").strip() for c in series_categories}

    # GH issue #5: archive a category the moment it's first seen on this
    # provider, same as Dispatcharr's own "auto-archive newly discovered VOD
    # provider categories" behavior, instead of it landing in the library
    # fully visible until an admin notices and hand-adds it to
    # import_exclude_categories. known_import_categories is always
    # refreshed below regardless of the setting, so turning this on later
    # never retroactively archives the whole existing category list.
    seen_category_names = {n for n in category_names.values() if n} | {n for n in series_category_names.values() if n}
    if provider.get("archive_new_categories"):
        known_categories = set(provider.get("known_import_categories") or [])
        new_categories = seen_category_names - known_categories
        if new_categories:
            exclude_categories = list(exclude_categories) + list(new_categories)
            logger.info("[vod_importer] provider=%s auto-archiving %d newly discovered categor(y/ies): %s",
                        provider["name"], len(new_categories), ", ".join(sorted(new_categories)))
    await asyncio.to_thread(
        vod_db.set_provider_known_import_categories, provider_id,
        sorted(seen_category_names | set(provider.get("known_import_categories") or [])),
    )

    streams = await client.get_vod_streams()
    movie_name_rules = await asyncio.to_thread(vod_db.get_active_rules_for_field, "movie", "name")
    movie_items = []
    for s in streams:
        name, year = parse_name_year(s.get("name") or "")
        name = vod_db.apply_rules_to_value(name, movie_name_rules)
        category_name = category_names.get(str(s.get("category_id")))
        movie_items.append({
            "name": name,
            "year": year,
            "provider_stream_id": str(s["stream_id"]),
            "container_extension": s.get("container_extension") or "mp4",
            "provider_category_name": category_name,
            # The provider's own unstripped name, before parse_name_year and
            # Title & Metadata Rules clean it up -- "4K"/"UHD"-style quality
            # markers commonly live in exactly the prefix those rules are
            # meant to strip, and movies.name is shared across every source
            # of this movie (that's what makes them the same movie), so it
            # can never differentiate between two sources' quality on its
            # own. This is the real per-source signal a quality-based stream
            # priority feature would need (see vod_manager-ghi).
            "raw_name": s.get("name") or "",
            "auto_archive": _should_auto_archive(name, category_name, exclude_categories, exclude_uncategorized),
            # Some providers' bulk get_vod_streams list already includes
            # this (confirmed live 2026-09-05: 3 of 5 real providers) --
            # capturing it lets enrich_movie's TMDB-first fallback kick in
            # from this movie's very first enrichment pass. No genre/cast/
            # plot in this endpoint though (unlike get_series), so nothing
            # else is worth capturing here.
            "tmdb_id": _clean_tmdb_id(s.get("tmdb")),
        })
    movie_result = await asyncio.to_thread(vod_db.bulk_import_movies, provider_id, movie_items)
    logger.info("[vod_importer] provider=%s movies: %s", provider["name"], movie_result)

    series_list = await client.get_series()
    series_name_rules = await asyncio.to_thread(vod_db.get_active_rules_for_field, "series", "name")
    # Most real XC panels' bulk get_series list already carries the same
    # detail fields enrich_series would otherwise pay a separate
    # get_series_info call per series to fetch (confirmed live 2026-09-05
    # against 5 of 6 real providers: plot, cast, director, genre, cover,
    # rating, tmdb all present). Rules fetched once here, not per item --
    # same reasoning as series_name_rules above, just extended to every
    # field bulk_import_series can now capture for free.
    detail_rules = {
        field: await asyncio.to_thread(vod_db.get_active_rules_for_field, "series", field)
        for field in ("genre", "description", "cast_list", "director")
    }
    series_items = []
    for s in series_list:
        name, year = parse_name_year(s.get("name") or "")
        name = vod_db.apply_rules_to_value(name, series_name_rules)
        category_name = series_category_names.get(str(s.get("category_id")))
        series_items.append({
            "name": name,
            "year": year or _coerce_year(s.get("year")),
            "provider_series_id": str(s["series_id"]),
            "provider_category_name": category_name,
            # See movie_items' identical raw_name field above -- the
            # provider's own unstripped name, before parse_name_year and
            # Title & Metadata Rules clean it up.
            "raw_name": s.get("name") or "",
            "auto_archive": _should_auto_archive(name, category_name, exclude_categories, exclude_uncategorized),
            "_has_detail": True,
            "genre": vod_db.apply_rules_to_value(s.get("genre") or None, detail_rules["genre"]),
            "description": vod_db.apply_rules_to_value(s.get("plot") or None, detail_rules["description"]),
            "cast_list": vod_db.apply_rules_to_value(s.get("cast") or None, detail_rules["cast_list"]),
            "director": vod_db.apply_rules_to_value(s.get("director") or None, detail_rules["director"]),
            "poster_url": s.get("cover") or None,
            "rating": s.get("rating") or None,
            "release_date": s.get("releaseDate") or s.get("release_date") or None,
            # Some providers send this under "tmdb", not "tmdb_id" -- see
            # enrich_series's identical comment for why both need checking.
            "tmdb_id": _clean_tmdb_id(s.get("tmdb")) or _clean_tmdb_id(s.get("tmdb_id")),
            "provider_last_modified": s.get("last_modified") or None,
        })
    series_result = await asyncio.to_thread(vod_db.bulk_import_series, provider_id, series_items)
    logger.info("[vod_importer] provider=%s series: %s", provider["name"], series_result)

    await asyncio.to_thread(vod_db.set_provider_import_totals, provider_id, len(streams), len(series_list))

    if provider.get("auto_create_categories"):
        try:
            created = await asyncio.to_thread(
                _auto_create_categories_from_provider,
                set(category_names.values()), "movie", exclude_categories,
            ) + await asyncio.to_thread(
                _auto_create_categories_from_provider,
                set(series_category_names.values()), "series", exclude_categories,
            )
            if created:
                logger.info("[vod_importer] provider=%s auto-created %d categor(y/ies) from its own category list", provider["name"], created)
        except Exception as exc:
            logger.warning("[vod_importer] provider=%s auto-create-categories failed: %s", provider["name"], exc)

    return {
        "provider": provider["name"],
        "movie_categories": len(categories),
        "series_categories": len(series_categories),
        **movie_result,
        **series_result,
    }


def _auto_create_categories_from_provider(category_names: set[str | None], content_type: str, exclude_categories: list[str]) -> int:
    """User request (Discord, KNM [BEES]/sjsteve, 2026-07-29): "the option at
    the provider connection to enable recreating the categories on import."
    Opt-in per provider (providers.auto_create_categories, default off).

    One VOD Manager smart category per distinct provider category name seen
    this import, matched via the provider_category rule field (see
    vod_db._SMART_CATEGORY_FIELDS) rather than scoped to just this one
    provider -- if two providers both have a category literally named
    "Comedy", they share the one VOD Manager "Comedy" category rather than
    creating "Comedy" and "Comedy (2)". upsert_category is itself an upsert
    keyed on (name, content_type), so re-running this on every import is a
    correct no-op for a category that already exists -- this only ever
    creates what's missing, never duplicates or overwrites an admin's own
    edits to an already-existing category with the same name.

    Evaluated immediately (not left for the next scheduled sweep) so the
    category shows real content right after the import that created it,
    same "auto-run on create" principle as vod_routes.upsert_category."""
    import json
    created = 0
    for name in category_names:
        name = (name or "").strip()
        if not name or name in exclude_categories:
            continue
        # Real bug caught live 2026-07-29, before this ever ran against real
        # data: upsert_category is a plain upsert that OVERWRITES rule_json
        # unconditionally on an existing row -- calling it here for a
        # category name that already exists (e.g. a user's own hand-built
        # "Music" category matched on genre) would have silently clobbered
        # their rule with this provider_category one. Skip entirely for an
        # existing category instead -- this only ever creates what's
        # missing, exactly as the module docstring always claimed but the
        # code didn't actually do until this fix.
        if vod_db.get_category_by_name(name, content_type):
            continue
        # Real race condition caught live 2026-07-29: the get_category_by_name
        # check above and upsert_category's own INSERT aren't atomic --
        # nothing here is unusual about that in isolation, except this
        # feature makes the SAME category name being auto-created from TWO
        # PROVIDERS AT ONCE a routine occurrence (e.g. the periodic catalog
        # refresher importing several providers back to back, more than one
        # with a "Comedy" category, or just two providers happening to
        # finish their own import right on top of each other) -- ordinary
        # sqlite3.IntegrityError on categories.name's UNIQUE constraint when
        # that happens, not exceptional. Catching it here and falling
        # through to re-fetch + evaluate (rather than letting it propagate
        # and abort every category still left in this loop) is what makes
        # this correct under real concurrent imports, not just a single one.
        try:
            category_id = vod_db.upsert_category(
                name, content_type, is_smart=True,
                rule_json=json.dumps({"match": "all", "conditions": [{"field": "provider_category", "op": "equals", "value": name}]}),
            )
            created += 1
        except sqlite3.IntegrityError:
            existing = vod_db.get_category_by_name(name, content_type)
            if not existing:
                # Lost the race in a way that isn't "someone else just made
                # it" (e.g. a genuinely different constraint) -- skip this
                # one name rather than crash the rest of the batch.
                logger.warning("[vod_importer] auto-create-categories: could not create or find %r (%s)", name, content_type)
                continue
            category_id = existing["id"]
        except sqlite3.OperationalError as exc:
            # Same reasoning as bulk_import_movies's own lock-retry handling
            # -- real concurrent-import contention observed live 2026-07-29
            # (heavy "database is locked" activity during simultaneous
            # imports). One category failing to create this pass isn't fatal
            # to the rest of the batch or the import itself; it'll pick up
            # on the next import/sweep.
            logger.warning("[vod_importer] auto-create-categories: %r (%s) hit %s, will retry next pass", name, content_type, exc)
            continue
        try:
            vod_db.evaluate_smart_category(category_id)
        except Exception as exc:
            logger.warning("[vod_importer] auto-created category=%s evaluate failed: %s", category_id, exc)
    return created


def _apply_field_rules(content_type: str, fields: dict) -> dict:
    """Applies each field's active metadata_rules (regex find/replace) to the
    freshly-fetched enrichment value before it's persisted."""
    result = {}
    for field, value in fields.items():
        rules = vod_db.get_active_rules_for_field(content_type, field)
        result[field] = vod_db.apply_rules_to_value(value, rules)
    return result


async def enrich_movie(movie_id: int, *, force: bool = False) -> bool:
    """Fetch get_vod_info for this movie's best source and persist detail
    fields. Returns False without a network call if already fresh (unless
    force=True) — the on-demand-and-cache pattern from the module docstring."""
    if not force and not await asyncio.to_thread(vod_db.movie_needs_enrichment, movie_id):
        return False

    sources = await asyncio.to_thread(vod_db.list_movie_sources, movie_id)
    if not sources:
        return False
    source = sources[0]
    provider = await asyncio.to_thread(vod_db.get_provider, source["provider_id"])
    if not provider:
        return False

    if provider.get("provider_type") == "plex":
        # Plex's library listing already hands back full detail at import
        # time (see plex_importer.py) — nothing more to lazily fetch here,
        # just refresh the TTL stamp so the scheduler leaves it alone.
        await asyncio.to_thread(vod_db.set_movie_enrichment, movie_id)
        return True

    if provider.get("provider_type") in ("emby", "jellyfin"):
        # Emby/Jellyfin's library listing already hands back everything
        # except People (see emby_vod_client.list_movies's docstring for why
        # that field is excluded from the bulk import) -- so this is the one
        # thing left to lazily backfill here, one item at a time instead of
        # for the whole library up front.
        async with emby_vod_client.EmbyVodClient(provider) as client:
            item = await client.get_movie_people(source["provider_stream_id"])
        fields = emby_vod_client.extract_common_fields(item)
        await asyncio.to_thread(
            vod_db.set_movie_enrichment, movie_id,
            **_apply_field_rules("movie", {
                "director": fields["director"],
                "cast_list": fields["cast_list"],
            }),
        )
        return True

    # If this movie already carries a confirmed tmdb_id (set by a previous
    # provider enrichment, or backfilled via Duplicate Finder's merge flow),
    # prefer TMDB directly over the provider -- same detail fields, but
    # against TMDB's own rate limit instead of this provider account's,
    # which is what bulk_enrich_all's backoff/adaptive-concurrency machinery
    # exists to protect. Only bitrate is skipped this way, since that's
    # per-SOURCE and only the provider's get_vod_info call can supply it.
    movie_row = await asyncio.to_thread(vod_db.get_movie, movie_id)
    existing_tmdb_id = movie_row.get("tmdb_id") if movie_row else None
    if existing_tmdb_id:
        tmdb_detail = await tmdb_sync.get_movie_full_details(existing_tmdb_id)
        if tmdb_detail:
            name_fields = {}
            if tmdb_detail.get("name"):
                name_rules = await asyncio.to_thread(vod_db.get_active_rules_for_field, "movie", "name")
                name_fields["name"] = vod_db.apply_rules_to_value(tmdb_detail["name"], name_rules)
            await asyncio.to_thread(
                vod_db.set_movie_enrichment,
                movie_id,
                **name_fields,
                **_apply_field_rules("movie", {
                    "genre": tmdb_detail.get("genre"),
                    "description": tmdb_detail.get("description"),
                    "cast_list": tmdb_detail.get("cast_list"),
                    "director": tmdb_detail.get("director"),
                    "country": tmdb_detail.get("country"),
                }),
                tmdb_id=existing_tmdb_id,
                poster_url=tmdb_detail.get("poster_url"),
                duration_secs=tmdb_detail.get("duration_secs"),
                rating=tmdb_detail.get("rating"),
                release_date=tmdb_detail.get("release_date"),
            )
            return True
        # TMDB lookup failed (no API key configured, bad id, TMDB down) --
        # fall through to the provider so this movie still gets enriched.

    client = XCProviderClient(provider)

    info = _as_dict(await client.get_vod_info(source["provider_stream_id"]))
    detail = _as_dict(info.get("info"))

    # Overwriting name with the provider's own clean title (e.g. "L.A.
    # Confidential (1997)" instead of the raw imported filename "123.L.A.
    # Confidential.1997") is only safe as of 2026-07-29's bulk_import_movies
    # rewrite: re-imports now match primarily by movie_sources.
    # (provider_id, provider_stream_id), not by re-deriving identity from
    # (name, year) every pass -- so changing name here no longer risks the
    # next scheduled refresh failing to find this row and creating a
    # duplicate. Applies the same user-configured title cleanup rules
    # (Title & Metadata Rules) that a fresh import already runs the name
    # through, so e.g. a "4K:" prefix-strip rule still applies here too.
    name_fields = {}
    if detail.get("name"):
        name_rules = await asyncio.to_thread(vod_db.get_active_rules_for_field, "movie", "name")
        name_fields["name"] = vod_db.apply_rules_to_value(detail["name"], name_rules)

    await asyncio.to_thread(
        vod_db.set_movie_enrichment,
        movie_id,
        **name_fields,
        **_apply_field_rules("movie", {
            "genre": detail.get("genre") or None,
            "description": detail.get("plot") or detail.get("description") or None,
            "cast_list": detail.get("cast") or detail.get("actors") or None,
            "director": detail.get("director") or None,
            "country": detail.get("country") or None,
        }),
        tmdb_id=_clean_tmdb_id(detail.get("tmdb_id")),
        poster_url=detail.get("cover_big") or detail.get("movie_image") or None,
        duration_secs=detail.get("duration_secs") or None,
        # rating/release_date not run through _apply_field_rules -- those
        # regex find/replace rules exist for cleaning up freeform text
        # (titles, descriptions), not for a numeric rating or an ISO date.
        rating=detail.get("rating") or None,
        release_date=detail.get("releasedate") or None,
    )
    # bitrate is per-SOURCE (see vod_db.set_movie_source_bitrate's docstring),
    # not per-movie -- this get_vod_info call was made against this specific
    # source, so it's the only one this bitrate value is actually true for.
    bitrate = _coerce_int(detail.get("bitrate"))
    if bitrate is not None:
        await asyncio.to_thread(vod_db.set_movie_source_bitrate, source["id"], bitrate)
    return True


async def enrich_series(series_id: int, *, force: bool = False) -> dict:
    """Fetch get_series_info -- this is the only source of episodes (most
    real XC panels' bulk get_series list already carries full series detail,
    see bulk_import_series, but never episodes), so this call is
    load-bearing even for a series whose detail fields are already fresh
    from the last bulk import. series_needs_enrichment gates this on the
    provider's own last_modified where available, not a blind TTL -- see its
    docstring -- so this mostly only actually runs for a series that's
    genuinely new or has reported a change.

    Returns {"fetched": bool, "reason": str | None} rather than a bare bool
    -- every False outcome used to look identical (nothing happened, no
    error), which meant a real problem (the provider this series was
    imported from got deleted since) was indistinguishable from "already up
    to date, nothing to do" from the caller's side. A caller like the
    year-review panel's "fetch episodes to preview" button needs to tell
    those apart to show something better than a spinner that just resets.

    Every vod_db call in here (and in enrich_movie above) is offloaded via
    asyncio.to_thread — these are plain synchronous sqlite3 calls, and
    calling them directly on the event loop thread means any lock
    contention (very real: bulk_enrich_all runs 8 of these concurrently
    against the same db file) freezes the ENTIRE process, including
    unrelated concurrent work like a video stream relay. That's what was
    causing playback to stall mid-stream even though the network path to
    the source was fine."""
    if not force and not await asyncio.to_thread(vod_db.series_needs_enrichment, series_id):
        return {"fetched": False, "reason": "already up to date"}

    series = await asyncio.to_thread(vod_db.get_series, series_id)
    if not series:
        return {"fetched": False, "reason": "series not found"}
    if not series.get("import_provider_id"):
        return {"fetched": False, "reason": "no source provider recorded for this series"}

    provider = await asyncio.to_thread(vod_db.get_provider, series["import_provider_id"])
    if not provider:
        return {"fetched": False, "reason": "the provider this series was originally imported from no longer exists"}

    if provider.get("provider_type") == "plex":
        # Same reasoning as enrich_movie: Plex already gave us full detail
        # and every episode at import time (plex_importer.py) — episodes
        # aren't lazily discovered here the way XC's are.
        await asyncio.to_thread(vod_db.set_series_enrichment, series_id)
        return {"fetched": True, "reason": None}

    client = XCProviderClient(provider)
    info = _as_dict(await client.get_series_info(str(series["import_provider_series_id"])))
    detail = _as_dict(info.get("info"))

    # See enrich_movie's identical comment -- safe as of 2026-07-29's
    # bulk_import_series rewrite, which now matches primarily by
    # (import_provider_id, import_provider_series_id), not by re-deriving
    # identity from (name, year) every pass.
    name_fields = {}
    if detail.get("name"):
        name_rules = await asyncio.to_thread(vod_db.get_active_rules_for_field, "series", "name")
        name_fields["name"] = vod_db.apply_rules_to_value(detail["name"], name_rules)

    await asyncio.to_thread(
        vod_db.set_series_enrichment,
        series_id,
        **name_fields,
        **_apply_field_rules("series", {
            "genre": detail.get("genre") or None,
            "description": detail.get("plot") or None,
            "cast_list": detail.get("cast") or None,
            "director": detail.get("director") or None,
            "country": detail.get("country") or None,
        }),
        # This provider sends the series' TMDB id under "tmdb", not "tmdb_id"
        # (unlike its own movie endpoint, which does use "tmdb_id") -- check
        # both since key naming isn't consistent even within one provider,
        # let alone across others.
        tmdb_id=_clean_tmdb_id(detail.get("tmdb")) or _clean_tmdb_id(detail.get("tmdb_id")),
        poster_url=detail.get("cover") or None,
        rating=detail.get("rating") or None,
        release_date=detail.get("releasedate") or None,
        # Snapshot of what bulk_import_series last saw for this series --
        # series_needs_enrichment compares the two on the next pass to know
        # whether this (expensive, per-series) call is worth making again.
        episodes_synced_last_modified=series.get("provider_last_modified"),
    )

    # get_series_info's "episodes" field is documented as {season_key: [ep, ...]}
    # (standard XC shape), but at least one real provider returns a plain
    # list of per-season lists instead — [[ep,...], [ep,...]].
    # Each episode also carries its own "season" field regardless of shape, so
    # trust that over the dict key / list index, falling back to the latter
    # only if a provider omits it.
    episodes_raw = info.get("episodes") or {}
    season_groups = episodes_raw.items() if isinstance(episodes_raw, dict) else enumerate(episodes_raw)

    for season_key, episodes in season_groups:
        for ep in episodes:
            season_number = ep.get("season", season_key)
            episode_id = await asyncio.to_thread(
                vod_db.add_episode,
                series_id,
                season_number=int(season_number),
                episode_number=int(ep.get("episode_num", 0)),
                name=ep.get("title") or f"Episode {ep.get('episode_num', '?')}",
                description=(ep.get("info") or {}).get("plot") or None,
                duration_secs=(ep.get("info") or {}).get("duration_secs") or None,
            )
            episode_source_id = await asyncio.to_thread(
                vod_db.add_episode_source,
                episode_id, provider["id"], str(ep["id"]),
                ep.get("container_extension") or "mp4",
                raw_name=ep.get("title") or None,
                # XC only reports category at the series level, not per-
                # episode -- bulk_import_series stamped it onto `series`
                # itself for exactly this moment, since episodes weren't
                # known yet back at that earlier, cheap bulk-list stage.
                # Real bug found live 2026-07-29: this was never threaded
                # through at all, so provider_category-based series
                # matching (evaluate_smart_category, auto-create-categories)
                # had no data to work with for any provider, ever.
                provider_category_name=series.get("provider_category_name"),
            )
            episode_bitrate = _coerce_int((ep.get("info") or {}).get("bitrate"))
            if episode_bitrate is not None:
                await asyncio.to_thread(vod_db.set_episode_source_bitrate, episode_source_id, episode_bitrate)

    return {"fetched": True, "reason": None}


# ── Bulk enrichment ──────────────────────────────────────────────────────────
# On-demand-and-cache (above) only ever touches one item per click. Bulk mode
# walks the whole pool with bounded concurrency so it doesn't hammer a real
# provider's API — progress is tracked in-process (single-instance app, no
# need for anything heavier) and polled from the UI rather than blocking a
# single request for what can be a multi-minute run across a large pool.

_ENRICH_PROGRESS: dict = {
    "running": False,
    "movies_total": 0, "movies_done": 0, "movies_errors": 0, "movies_backoff_skipped": 0,
    "series_total": 0, "series_done": 0, "series_errors": 0, "series_backoff_skipped": 0,
    "started_at": None, "finished_at": None,
}


def get_enrich_progress() -> dict:
    progress = dict(_ENRICH_PROGRESS)
    # Surfaces which provider(s), if any, enrichment is currently backing off
    # from and for how much longer -- otherwise a stalled-looking done-count
    # (see _record_provider_failure's docstring) has no visible explanation
    # in the UI beyond "it's slow".
    progress["providers_backing_off"] = [
        {"provider_id": pid, "seconds_remaining": round(remaining, 1)}
        for pid, remaining in ((pid, _provider_backoff_remaining(pid)) for pid in list(_PROVIDER_BACKOFF))
        if remaining > 0
    ]
    # Only surfaces a provider actually narrowed below the default -- most
    # runs never show anything here, same as providers_backing_off above.
    progress["providers_throttled"] = [
        {"provider_id": pid, "concurrency": limiter.cap, "max_concurrency": _PROVIDER_MAX_CONCURRENCY}
        for pid, limiter in list(_PROVIDER_LIMITERS.items())
        if limiter.cap < _PROVIDER_MAX_CONCURRENCY
    ]
    return progress


_PROGRESS_PREFIX = {"movie": "movies", "series": "series"}  # "series" pluralizes to itself, not "seriess"


async def _enrich_one(kind: str, sem: asyncio.Semaphore, item_id: int, force: bool) -> None:
    prefix = _PROGRESS_PREFIX[kind]
    async with sem:
        try:
            if kind == "movie":
                await enrich_movie(item_id, force=force)
            else:
                await enrich_series(item_id, force=force)
        except ProviderBackoffError:
            # Not a real failure -- deliberately skipped, no request sent,
            # because that item's provider is already known to be
            # rate-limited/blocked right now (see _record_provider_failure).
            # Doesn't touch last_enriched_at, so this item stays eligible
            # and gets picked up again on the next bulk-enrich run (or later
            # in this same run, once the provider's backoff expires).
            _ENRICH_PROGRESS[f"{prefix}_backoff_skipped"] += 1
        except Exception as exc:
            # httpx.HTTPStatusError/ConnectError's own str() embeds the full
            # request URL -- real, working provider credentials included --
            # so this must go through the same redaction xc_server already
            # uses for stream URLs, or a paid-subscription login lands in
            # plaintext in container logs on every single failed lookup
            # (real user log 2026-09-05: hundreds of these per bulk-enrich
            # run, one per 404/429). tmdb_sync._redact is this same fix for
            # TMDB's own api_key query param.
            logger.warning("[vod_importer] bulk enrich %s=%s failed: %s", kind, item_id, _redact_upstream_url(str(exc)))
            _ENRICH_PROGRESS[f"{prefix}_errors"] += 1
        finally:
            _ENRICH_PROGRESS[f"{prefix}_done"] += 1


async def bulk_enrich_all(concurrency: int = 8, force: bool = False) -> None:
    """Enriches every movie and series in the pool, movies and series running
    CONCURRENTLY -- each kind gets its own `concurrency`-sized semaphore, so
    a large movie catalog can never starve series out of running entirely.

    Used to run movies-to-completion, then series, as two sequential
    gather() batches. Real bug found live 2026-08-20: a large movie catalog
    (230k+ movies) took long enough -- especially with a flaky provider
    throwing periodic 502s/connection failures along the way -- that the
    process restarted before the movie batch ever finished, so the series
    batch never even started. Every series enrich_series() call is also
    where a series' episodes come from (see that function's docstring), so
    this wasn't just delayed metadata -- it meant zero episodes for the
    entire TV library, indefinitely, until a single enrich run survived
    long enough to get all the way through movies first."""
    if _ENRICH_PROGRESS["running"]:
        return

    movie_ids  = await asyncio.to_thread(vod_db.list_all_movie_ids)
    series_ids = await asyncio.to_thread(vod_db.list_all_series_ids)
    _ENRICH_PROGRESS.update({
        "running": True,
        "movies_total": len(movie_ids), "movies_done": 0, "movies_errors": 0, "movies_backoff_skipped": 0,
        "series_total": len(series_ids), "series_done": 0, "series_errors": 0, "series_backoff_skipped": 0,
        "started_at": time.time(), "finished_at": None,
    })
    logger.info("[vod_importer] bulk enrich starting: %d movies, %d series, concurrency=%d",
                len(movie_ids), len(series_ids), concurrency)

    # Separate semaphores -- movies and series shouldn't compete with each
    # other for the same `concurrency` slots (that would just reproduce the
    # starvation this is fixing, only softer), each kind gets its own
    # provider-request budget.
    movie_sem = asyncio.Semaphore(concurrency)
    series_sem = asyncio.Semaphore(concurrency)
    try:
        # return_exceptions=True: _enrich_one already catches everything it can
        # anticipate, but a single unanticipated exception must not abort the
        # rest of the batch (gather() without this re-raises immediately on
        # the first failure, leaving every other in-flight task orphaned).
        await asyncio.gather(
            *(_enrich_one("movie", movie_sem, mid, force) for mid in movie_ids),
            *(_enrich_one("series", series_sem, sid, force) for sid in series_ids),
            return_exceptions=True,
        )
    finally:
        _ENRICH_PROGRESS["running"] = False
        _ENRICH_PROGRESS["finished_at"] = time.time()
        elapsed = _ENRICH_PROGRESS["finished_at"] - _ENRICH_PROGRESS["started_at"]
        logger.info(
            "[vod_importer] bulk enrich done in %.1fs: movies %d/%d (%d errors, %d backoff-skipped), "
            "series %d/%d (%d errors, %d backoff-skipped)",
            elapsed,
            _ENRICH_PROGRESS["movies_done"], _ENRICH_PROGRESS["movies_total"], _ENRICH_PROGRESS["movies_errors"],
            _ENRICH_PROGRESS["movies_backoff_skipped"],
            _ENRICH_PROGRESS["series_done"], _ENRICH_PROGRESS["series_total"], _ENRICH_PROGRESS["series_errors"],
            _ENRICH_PROGRESS["series_backoff_skipped"],
        )


async def resweep_smart_categories() -> None:
    """Re-evaluate every smart category with a rule configured (see
    vod_db.list_smart_category_ids_with_rules) so newly imported content
    actually shows up in them without a manual "evaluate" click -- broadened
    2026-07-29 from catch-all-only per user direction ("categories in
    general need some sort of sweep... on whatever the normal provider list
    update is"): rule-based evaluation is free (no external API call) and
    purely additive, so there's no real downside to keeping every rule
    fresh by default rather than requiring an explicit opt-in schedule for
    each one. (AI-assisted evaluation stays opt-in-only, see
    main.py._smart_category_scheduler -- that one has real recurring cost.)

    Shared by the periodic catalog refresher (main.py, fires right after a
    provider's own due catalog refresh -- "whatever the normal provider list
    update is"), the manual "Import catalog" button (vod_routes.py), the
    independent fixed-interval sweep (main.py._uncategorized_sweep_loop),
    and apply_exclusions_job.py -- calling it from all of them means none of
    those paths has to wait on another one to be reflected.

    Also retroactively purges any already-review_excluded item still sitting
    in a category (vod_db.purge_excluded_from_categories) -- covers an
    install that ran import-time exclusion before that bug was fixed, so
    already-wrongly-placed rows actually get cleaned up here rather than
    needing a separate one-off action."""
    try:
        purge_result = await asyncio.to_thread(vod_db.purge_excluded_from_categories)
        if purge_result["movies_removed"] or purge_result["series_removed"]:
            logger.info("[vod_importer] purged already-excluded items from categories: %s", purge_result)
    except Exception as exc:
        logger.warning("[vod_importer] purge_excluded_from_categories failed: %s", exc)

    for category_id in await asyncio.to_thread(vod_db.list_smart_category_ids_with_rules):
        try:
            result = await asyncio.to_thread(vod_db.evaluate_smart_category, category_id)
            logger.info("[vod_importer] smart category=%s: %s", category_id, result)
        except Exception as exc:
            logger.warning("[vod_importer] smart category=%s failed: %s", category_id, exc)
