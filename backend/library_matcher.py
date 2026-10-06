"""TMDB matching for library-source items (see library_parser for path parsing).

Deliberately cheaper and stricter than tmdb_sync.search_title: that helper makes
up to 6 requests per lookup (search + a detail call per candidate) and takes no
year, which is fine for a human-driven review flow but far too heavy for
matching thousands of files. This module does ONE search request per title
(two at most, when a year-filtered search comes back empty), and never guesses:

  matched    -- an explicit id, or an exact normalized title whose year agrees
                (or is the only exact-title candidate when no year is known)
  ambiguous  -- several plausible candidates; carries them for the review queue
  unmatched  -- nothing plausible, or TMDB isn't configured / errored

A wrong silent match is worse than an unmatched item, so anything short of an
exact normalized title is never auto-applied.
"""

import asyncio
import json
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import tmdb_store
import tmdb_sync
import vod_db
from config import get_tmdb_api_key
from library_parser import ParsedPath

logger = logging.getLogger(__name__)

_MAX_CANDIDATES = 5


@dataclass
class Candidate:
    tmdb_id: str
    title: str
    year: int | None
    popularity: float = 0.0


@dataclass
class MatchResult:
    status: str                    # "matched" | "ambiguous" | "unmatched"
    tmdb_id: str | None = None
    title: str | None = None
    year: int | None = None
    confidence: str | None = None  # "explicit_id" | "title_year" | "title_only"
    candidates: list[Candidate] = field(default_factory=list)
    reason: str | None = None
    transient: bool = False        # lookup failed (network/key); never persisted, retried next scan


# An unmatched (no TMDB results) decision is retried after this long, since
# TMDB gains titles over time; matched/ambiguous decisions are kept until the
# path is renamed or a human changes them.
_UNMATCHED_RETRY = timedelta(days=7)


def normalize_title(s: str) -> str:
    """Accent/case/punctuation-insensitive key: "Amélie" == "Amelie",
    "Spider-Man: Homecoming" == "spider man homecoming", "&" == "and", and a
    leading "The" is ignored ("The Office" == "Office")."""
    s = "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))
    s = s.lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    return re.sub(r"^(the|a|an) ", "", s)


def _year_of(date: str | None) -> int | None:
    return int(date[:4]) if date and len(date) >= 4 and date[:4].isdigit() else None


def _to_candidates(results: list[dict], content_type: str) -> list[Candidate]:
    out = []
    for item in results:
        if item.get("id") is None:
            continue
        if content_type == "movie":
            title, date = item.get("title") or item.get("original_title"), item.get("release_date")
        else:
            title, date = item.get("name") or item.get("original_name"), item.get("first_air_date")
        if not title:
            continue
        out.append(Candidate(str(item["id"]), title, _year_of(date), float(item.get("popularity") or 0.0)))
    return out


async def _search(query: str, content_type: str, year: int | None) -> list[Candidate]:
    api_key = get_tmdb_api_key()
    if not api_key:
        raise ValueError("TMDB API key not configured")
    endpoint = "movie" if content_type == "movie" else "tv"
    # Local library first; only trusted when an exact-name hit also agrees on year.
    hits = await tmdb_sync.search_local(endpoint, query, year)
    if hits and (year is None or any(
        h["exact"] and h["year"] is not None and abs(h["year"] - year) <= 1 for h in hits
    )):
        await tmdb_sync.note_lookup(True)
        return _to_candidates([tmdb_sync.as_search_item(endpoint, h) for h in hits], content_type)
    await tmdb_sync.note_lookup(False)
    params = {"api_key": api_key, "query": query}
    if year:
        params["year" if content_type == "movie" else "first_air_date_year"] = year
    async with tmdb_sync._tmdb_semaphore:
        r = await tmdb_sync._tmdb_get(f"{tmdb_sync._API_BASE}/search/{endpoint}", params)
    r.raise_for_status()
    return _to_candidates(r.json().get("results", []), content_type)


async def _lookup_by_imdb(imdb_id: str, content_type: str) -> Candidate | None:
    api_key = get_tmdb_api_key()
    if not api_key:
        raise ValueError("TMDB API key not configured")
    endpoint = "movie" if content_type == "movie" else "tv"
    if tmdb_sync.store_enabled():
        try:
            row = await asyncio.to_thread(tmdb_store.find_by_imdb, endpoint, imdb_id)
        except Exception as exc:
            logger.warning("[library_matcher] local IMDb lookup failed for %s: %s", imdb_id, exc)
            row = None
        if row and (row["title"] or row["original_title"]):
            await tmdb_sync.note_lookup(True)
            return Candidate(str(row["tmdb_id"]), row["title"] or row["original_title"], row["year"],
                             float(row["popularity"] or 0.0))
    await tmdb_sync.note_lookup(False)
    async with tmdb_sync._tmdb_semaphore:
        r = await tmdb_sync._tmdb_get(
            f"{tmdb_sync._API_BASE}/find/{imdb_id}",
            {"api_key": api_key, "external_source": "imdb_id"},
        )
    r.raise_for_status()
    data = r.json()
    key = "movie_results" if content_type == "movie" else "tv_results"
    found = _to_candidates(data.get(key, []), content_type)
    return found[0] if found else None


def decide(title: str, year: int | None, candidates: list[Candidate]) -> MatchResult:
    """Pure scoring step, split out so it is testable without any network."""
    key = normalize_title(title)
    exact = [c for c in candidates if normalize_title(c.title) == key]

    if year is not None:
        agreeing = [c for c in exact if c.year is not None and abs(c.year - year) <= 1]
        if len(agreeing) == 1:
            c = agreeing[0]
            return MatchResult("matched", c.tmdb_id, c.title, c.year, "title_year")
        if len(agreeing) > 1:
            # Same title within a year of each other (remakes/re-releases): prefer
            # the exact-year one only if it is unique, else send to review.
            same = [c for c in agreeing if c.year == year]
            if len(same) == 1:
                c = same[0]
                return MatchResult("matched", c.tmdb_id, c.title, c.year, "title_year")
            return MatchResult("ambiguous", candidates=agreeing[:_MAX_CANDIDATES], reason="several exact title+year matches")
    elif len(exact) == 1:
        c = exact[0]
        return MatchResult("matched", c.tmdb_id, c.title, c.year, "title_only")

    if candidates:
        pool = exact or candidates
        reason = "exact title but year disagrees" if exact and year is not None else (
            "several exact title matches, no year" if len(exact) > 1 else "no exact title match")
        return MatchResult("ambiguous", candidates=pool[:_MAX_CANDIDATES], reason=reason)
    return MatchResult("unmatched", reason="no TMDB results")


async def match(parsed: ParsedPath) -> MatchResult:
    content_type = "movie" if parsed.kind == "movie" else "series"

    if parsed.tmdb_id:
        return MatchResult("matched", parsed.tmdb_id, parsed.title or None, parsed.year, "explicit_id")
    try:
        if parsed.imdb_id:
            found = await _lookup_by_imdb(parsed.imdb_id, content_type)
            if found:
                return MatchResult("matched", found.tmdb_id, found.title, found.year, "explicit_id")
    except Exception as exc:
        logger.warning("[library_matcher] TMDB match failed for %r: %s", parsed.title, tmdb_sync._redact(exc))
        return MatchResult("unmatched", reason="TMDB lookup failed", transient=True)
    if not parsed.title:
        return MatchResult("unmatched", reason="no title parsed from path")
    return await match_title(parsed.title, content_type, parsed.year)


# Split out of match() so catalog titles with no TMDB ID can
# be auto-matched by name+year too (tmdb_automatch).
async def match_title(title: str, content_type: str, year: int | None) -> MatchResult:
    """Name(+year) -> TMDB under decide()'s never-guess rules; local library
    first, TMDB /search on a local miss. content_type is 'movie' or 'series'."""
    try:
        candidates = await _search(title, content_type, year)
        if not candidates and year:
            # Year-filtered search can miss (TMDB dates differ by region/festival
            # release); retry unfiltered and let decide() enforce the +/-1 year.
            candidates = await _search(title, content_type, None)
        return decide(title, year, candidates)
    except Exception as exc:
        logger.warning("[library_matcher] TMDB match failed for %r: %s", title, tmdb_sync._redact(exc))
        return MatchResult("unmatched", reason="TMDB lookup failed", transient=True)


class ShowMatchCache:
    """One TMDB resolution per series folder instead of per episode file --
    cuts TV lookups from thousands to one per show, and guarantees every
    episode of a show lands on the same series rather than each being matched
    independently (which can split one show across different TMDB entries)."""

    def __init__(self) -> None:
        self._results: dict[str, MatchResult] = {}
        self.resolved: dict[tuple, MatchResult] = {}   # per-scan memo used by resolve()

    async def match_episode(self, parsed: ParsedPath) -> MatchResult:
        key = parsed.show_key or parsed.title.lower()
        if key not in self._results:
            self._results[key] = await match(parsed)
        return self._results[key]


def _query_of(parsed: ParsedPath) -> str:
    return f"{parsed.title}|{parsed.year or ''}"


def _from_row(row: dict) -> MatchResult:
    cands = [Candidate(**c) for c in json.loads(row["candidates_json"])] if row.get("candidates_json") else []
    return MatchResult(row["status"], row["tmdb_id"], row["title"], row["year"], row["confidence"],
                       cands, row["reason"])


def _row_is_reusable(row: dict | None, query: str) -> bool:
    if not row:
        return False
    if row["source"] == "manual":
        return True                      # a human's pick applies regardless of parsed query
    if row["query"] != query:
        return False                     # path renamed since the decision was made
    if row["status"] == "unmatched":
        try:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(row["updated_at"])
        except (ValueError, TypeError):
            return False
        return age < _UNMATCHED_RETRY
    return True


async def resolve(provider_id: int, rel_path: str, parsed: ParsedPath, cache: ShowMatchCache) -> MatchResult:
    """Persisted, cached entry point used by the library importer: reuses the
    stored decision when still valid, otherwise matches against TMDB (one call
    per show for TV) and stores the outcome. Explicit-id paths are cheap and
    self-describing, so they are stored like any other decision."""
    is_episode = parsed.kind == "episode"
    kind = "series" if is_episode else "movie"
    key = (parsed.show_key or parsed.title.lower()) if is_episode else rel_path
    query = _query_of(parsed)

    session_key = (provider_id, kind, key)
    if session_key in cache.resolved:
        return cache.resolved[session_key]

    row = await asyncio.to_thread(vod_db.get_library_match, provider_id, key, kind)
    if _row_is_reusable(row, query):
        result = _from_row(row)
    else:
        result = await match(parsed)
        if not result.transient:
            await asyncio.to_thread(
                lambda: vod_db.upsert_library_match(
                    provider_id, key, kind, status=result.status, query=query,
                    tmdb_id=result.tmdb_id, title=result.title, year=result.year,
                    confidence=result.confidence, reason=result.reason,
                    candidates_json=json.dumps([c.__dict__ for c in result.candidates]) if result.candidates else None,
                )
            )
            # A manual row blocked the write -> honour it rather than the auto result.
            latest = await asyncio.to_thread(vod_db.get_library_match, provider_id, key, kind)
            if latest and latest["source"] == "manual":
                result = _from_row(latest)
    cache.resolved[session_key] = result
    return result
