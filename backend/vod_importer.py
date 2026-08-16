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
import vod_db


def _should_auto_archive(name: str, provider_category_name: str | None, provider_exclude_categories: list[str]) -> bool:
    """Import-time equivalent of the manual Language Filter archive tool --
    deliberately NOT sibling-safe (see USERGUIDE's Language Filter section
    for that tool's "don't archive the only copy" behavior): an explicit
    exclusion rule means "I don't want this content in my library at all",
    not "prefer another language's copy if one exists". Language rules are
    global (config.get_import_language_exclusion); category rules are
    per-provider (providers.import_exclude_categories), since available
    categories genuinely differ provider to provider."""
    lang = config.get_import_language_exclusion()
    if lang["exclude_prefixes"]:
        code = vod_db._name_prefix_code(name)
        if code and code in lang["exclude_prefixes"]:
            return True
    if lang["exclude_non_latin"] and vod_db._is_non_latin_name(name):
        return True
    if provider_category_name and provider_category_name in provider_exclude_categories:
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


class XCProviderClient:
    def __init__(self, provider: dict):
        self.provider = provider
        self.base_url = provider["base_url"].rstrip("/")
        self.username = provider["username"]
        self.password = provider["password"]
        custom_ua = provider.get("custom_user_agent")
        self.headers = {"User-Agent": custom_ua} if custom_ua else _UPSTREAM_HEADERS

    async def _call(self, action: str | None = None, **params) -> object:
        query = {"username": self.username, "password": self.password}
        if action:
            query["action"] = action
        query.update(params)
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True, headers=self.headers) as client:
            r = await client.get(f"{self.base_url}/player_api.php", params=query)
            r.raise_for_status()
            return r.json()

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

    categories = await client.get_vod_categories()
    category_names = {str(c["category_id"]): c["category_name"] for c in categories}

    series_categories = await client.get_series_categories()
    series_category_names = {str(c["category_id"]): c["category_name"] for c in series_categories}

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
            "auto_archive": _should_auto_archive(name, category_name, exclude_categories),
        })
    movie_result = await asyncio.to_thread(vod_db.bulk_import_movies, provider_id, movie_items)
    logger.info("[vod_importer] provider=%s movies: %s", provider["name"], movie_result)

    series_list = await client.get_series()
    series_name_rules = await asyncio.to_thread(vod_db.get_active_rules_for_field, "series", "name")
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
            "auto_archive": _should_auto_archive(name, category_name, exclude_categories),
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
        tmdb_id=detail.get("tmdb_id") or None,
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
    """Fetch get_series_info — this is also where episodes come from (the
    bulk get_series list is series-metadata-only, no episodes), so this call
    is load-bearing even just to populate episodes, not only for detail.

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
        tmdb_id=detail.get("tmdb") or detail.get("tmdb_id") or None,
        poster_url=detail.get("cover") or None,
        rating=detail.get("rating") or None,
        release_date=detail.get("releasedate") or None,
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
    "movies_total": 0, "movies_done": 0, "movies_errors": 0,
    "series_total": 0, "series_done": 0, "series_errors": 0,
    "started_at": None, "finished_at": None,
}


def get_enrich_progress() -> dict:
    return dict(_ENRICH_PROGRESS)


_PROGRESS_PREFIX = {"movie": "movies", "series": "series"}  # "series" pluralizes to itself, not "seriess"


async def _enrich_one(kind: str, sem: asyncio.Semaphore, item_id: int, force: bool) -> None:
    prefix = _PROGRESS_PREFIX[kind]
    async with sem:
        try:
            if kind == "movie":
                await enrich_movie(item_id, force=force)
            else:
                await enrich_series(item_id, force=force)
        except Exception as exc:
            logger.warning("[vod_importer] bulk enrich %s=%s failed: %s", kind, item_id, exc)
            _ENRICH_PROGRESS[f"{prefix}_errors"] += 1
        finally:
            _ENRICH_PROGRESS[f"{prefix}_done"] += 1


async def bulk_enrich_all(concurrency: int = 8, force: bool = False) -> None:
    """Enriches every movie and series in the pool. Movies first, then series
    — each batch runs at bounded concurrency so we're never hitting a single
    provider with more than `concurrency` simultaneous requests."""
    if _ENRICH_PROGRESS["running"]:
        return

    movie_ids  = await asyncio.to_thread(vod_db.list_all_movie_ids)
    series_ids = await asyncio.to_thread(vod_db.list_all_series_ids)
    _ENRICH_PROGRESS.update({
        "running": True,
        "movies_total": len(movie_ids), "movies_done": 0, "movies_errors": 0,
        "series_total": len(series_ids), "series_done": 0, "series_errors": 0,
        "started_at": time.time(), "finished_at": None,
    })
    logger.info("[vod_importer] bulk enrich starting: %d movies, %d series, concurrency=%d",
                len(movie_ids), len(series_ids), concurrency)

    sem = asyncio.Semaphore(concurrency)
    try:
        # return_exceptions=True: _enrich_one already catches everything it can
        # anticipate, but a single unanticipated exception must not abort the
        # rest of the batch (gather() without this re-raises immediately on
        # the first failure, leaving every other in-flight task orphaned).
        await asyncio.gather(*(_enrich_one("movie", sem, mid, force) for mid in movie_ids), return_exceptions=True)
        await asyncio.gather(*(_enrich_one("series", sem, sid, force) for sid in series_ids), return_exceptions=True)
    finally:
        _ENRICH_PROGRESS["running"] = False
        _ENRICH_PROGRESS["finished_at"] = time.time()
        elapsed = _ENRICH_PROGRESS["finished_at"] - _ENRICH_PROGRESS["started_at"]
        logger.info(
            "[vod_importer] bulk enrich done in %.1fs: movies %d/%d (%d errors), series %d/%d (%d errors)",
            elapsed,
            _ENRICH_PROGRESS["movies_done"], _ENRICH_PROGRESS["movies_total"], _ENRICH_PROGRESS["movies_errors"],
            _ENRICH_PROGRESS["series_done"], _ENRICH_PROGRESS["series_total"], _ENRICH_PROGRESS["series_errors"],
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
