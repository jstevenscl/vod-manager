"""
Assigns TMDB IDs to catalog titles that have none.

Provider catalogs carry plenty of titles with a clean name+year but no TMDB
id; without one they can't be merged across providers (duplicate cards in
Dispatcharr) or enriched. Each pass matches a batch of them by name+year
through library_matcher's never-guess rules -- local TMDB library first, TMDB
/search on a local miss:
  - matched   -> the ID is set (merge-safe, as a reviewer would);
  - ambiguous -> listed in Metadata Review with the candidates, nothing set;
  - unmatched -> retried after a week (TMDB gains titles over time);
  - a failed lookup (network, rate limit) isn't recorded and is retried next pass.
"""

import asyncio
import logging

import library_matcher
import tmdb_store
import vod_db
from config import get_tmdb_store_settings

logger = logging.getLogger(__name__)


async def _unique_on_tmdb(content_type: str, name: str) -> bool:
    """A no-year match is only safe when TMDB lists exactly one title under
    that exact name -- a same-named title can otherwise be missed."""
    media_type = "movie" if content_type == "movie" else "tv"
    try:
        return await asyncio.to_thread(tmdb_store.export_name_count, media_type, name) == 1
    except Exception as exc:
        logger.warning("[tmdb_automatch] export name count failed for %r: %s", name, exc)
        return False


async def run_auto_match(limit: int | None = None) -> dict:
    """One pass over up to `limit` no-ID titles, series first."""
    if limit is None:
        limit = get_tmdb_store_settings()["auto_match_batch"]
    counts = {"checked": 0, "matched": 0, "ambiguous": 0, "unmatched": 0, "failed": 0}
    for content_type in ("series", "movie"):
        remaining = limit - counts["checked"]
        if remaining <= 0:
            break
        work = await asyncio.to_thread(vod_db.list_auto_match_work, content_type, remaining)
        for item in work:
            counts["checked"] += 1
            result = await library_matcher.match_title(item["name"], content_type, item["year"])
            if result.transient:
                counts["failed"] += 1
                continue
            if result.status == "matched" and result.confidence == "title_only" \
                    and not await _unique_on_tmdb(content_type, item["name"]):
                result = library_matcher.MatchResult(
                    "ambiguous", reason="no year, and TMDB has other titles with this name",
                    candidates=[library_matcher.Candidate(result.tmdb_id, result.title, result.year, 0.0)])
            if result.status == "matched":
                try:
                    await asyncio.to_thread(vod_db.set_tmdb_id, content_type, item["id"], result.tmdb_id, manual=False)
                except ValueError:  # item merged/removed since the batch was listed
                    continue
                counts["matched"] += 1
                continue
            candidates = [{"tmdb_id": c.tmdb_id, "title": c.title, "year": c.year} for c in result.candidates]
            await asyncio.to_thread(vod_db.record_auto_match, content_type, item["id"], result.status,
                                    result.reason, candidates)
            counts[result.status] += 1
    if counts["checked"]:
        logger.info("[tmdb_automatch] %s", counts)
    return counts
