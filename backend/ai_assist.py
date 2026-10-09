"""
AI-assisted category creation, Needs Review disambiguation, and missing-
artwork matching, via whichever AI provider the user has configured
(Anthropic, OpenAI, or Gemini -- see config.get_ai_provider). Three distinct
capabilities, matched to different cost/reliability profiles:

- Light mode (suggest_category_rule): one AI call translates a plain-
  English description into the existing smart-category rule_json schema
  (see vod_db.py's _rule_matches) -- cheap, and the result is just a
  starting point the user reviews/edits in the normal rule editor before
  saving, same as if they'd written the JSON by hand. Nothing is ever
  auto-saved from this call.
- Heavy mode (evaluate_candidates_for_category): for criteria the rule
  schema's handful of fields genuinely can't express (mood, plot elements,
  audience fit -- there's no "keyword" or "cast" field), the AI judges
  actual titles instead of field rules. Real per-item API cost, so this
  always runs over a *bounded* candidate set built by vod_db.get_ai_candidate_rows
  (an optional rule_json pre-filter, capped at a limit), never the raw
  pool, and batches many titles per call rather than one call each.
- suggest_year_review_match: picks the most likely correct match among TMDB
  search candidates already fetched for one Needs Review or missing-artwork
  item, with reasoning. Always a suggestion the reviewer still has to click
  to accept (see vod_routes.py's /needs-review/.../resolve/ and
  /missing-artwork/.../resolve/) -- nothing here ever resolves a match on
  its own.

Every call goes through _call_ai(), which forces a structured tool/function
call on whichever provider is active so the response is always the declared
JSON schema, never freeform prose to parse.
"""

import asyncio
import json
import logging

import httpx

from config import get_ai_model, get_ai_provider, get_anthropic_api_key, get_gemini_api_key, get_openai_api_key

logger = logging.getLogger(__name__)

_ANTHROPIC_API_BASE = "https://api.anthropic.com/v1/messages"
_ANTHROPIC_VERSION = "2023-06-01"
_OPENAI_API_BASE = "https://api.openai.com/v1/chat/completions"
_GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"

_RULE_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "A short, human-friendly category name for this rule."},
        "match": {"type": "string", "enum": ["all", "any"]},
        "conditions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "field": {"type": "string", "enum": ["name", "genre", "year", "country", "language", "director", "is_adult", "provider_category", "content_rating"]},
                    "op": {"type": "string", "enum": ["contains", "starts_with", "equals", "gte", "lte"]},
                    "value": {"type": "string"},
                },
                "required": ["field", "op", "value"],
            },
        },
    },
    "required": ["name", "match", "conditions"],
}

_BATCH_SCHEMA = {
    "type": "object",
    "properties": {
        "matches": {
            "type": "array",
            "items": {"type": "integer"},
            "description": "The numeric ids (from the numbered list) of every title that genuinely fits the description.",
        },
    },
    "required": ["matches"],
}

_YEAR_MATCH_SCHEMA = {
    "type": "object",
    "properties": {
        "best_match_index": {
            "type": ["integer", "null"],
            "description": "Index into the candidate list of the best match, or null if none are a confident match.",
        },
        "reasoning": {"type": "string"},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
    },
    "required": ["best_match_index", "reasoning", "confidence"],
}

_DUPLICATE_VERIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "same_title": {"type": "boolean", "description": "True if these are genuinely the same real movie/show, not just a coincidental name+year match."},
        "reasoning": {"type": "string"},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
    },
    "required": ["same_title", "reasoning", "confidence"],
}

_AI_EVAL_BATCH_SIZE = 30


async def _call_anthropic(model: str, system: str, user_message: str, tool_name: str, tool_description: str, schema: dict, max_tokens: int) -> dict:
    api_key = get_anthropic_api_key()
    if not api_key:
        raise ValueError("Anthropic API key not configured")

    body = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user_message}],
        "tools": [{"name": tool_name, "description": tool_description, "input_schema": schema}],
        "tool_choice": {"type": "tool", "name": tool_name},
    }
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.post(_ANTHROPIC_API_BASE, json=body, headers={
            "x-api-key": api_key,
            "anthropic-version": _ANTHROPIC_VERSION,
            "content-type": "application/json",
        })
        r.raise_for_status()
        data = r.json()

    for block in data.get("content", []):
        if block.get("type") == "tool_use" and block.get("name") == tool_name:
            return block["input"]
    raise ValueError("Claude did not return a structured response")


async def _call_openai(model: str, system: str, user_message: str, tool_name: str, tool_description: str, schema: dict, max_tokens: int) -> dict:
    api_key = get_openai_api_key()
    if not api_key:
        raise ValueError("OpenAI API key not configured")

    body = {
        "model": model,
        "max_completion_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_message},
        ],
        "tools": [{
            "type": "function",
            "function": {"name": tool_name, "description": tool_description, "parameters": schema},
        }],
        "tool_choice": {"type": "function", "function": {"name": tool_name}},
    }
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.post(_OPENAI_API_BASE, json=body, headers={
            "Authorization": f"Bearer {api_key}",
            "content-type": "application/json",
        })
        r.raise_for_status()
        data = r.json()

    try:
        call = data["choices"][0]["message"]["tool_calls"][0]
        return json.loads(call["function"]["arguments"])
    except (KeyError, IndexError, json.JSONDecodeError) as exc:
        raise ValueError("OpenAI did not return a structured response") from exc


_GEMINI_TRANSIENT_RETRY_DELAYS = (2.0, 5.0)


def _gemini_schema(schema):
    """A copy of `schema` in the OpenAPI subset Gemini's function declarations
    accept. Gemini rejects a list for `type` ("Schema type must not be a
    list", GH#51) and wants `nullable: true` instead, and does not take
    `additionalProperties`. Our shared schemas use standard JSON Schema
    (e.g. ["integer", "null"]), which Anthropic/OpenAI accept as-is."""
    if isinstance(schema, list):
        return [_gemini_schema(item) for item in schema]
    if not isinstance(schema, dict):
        return schema
    out = {}
    for key, value in schema.items():
        if key == "additionalProperties":
            continue
        if key == "type" and isinstance(value, list):
            kinds = [kind for kind in value if kind != "null"]
            if "null" in value:
                out["nullable"] = True
            out["type"] = kinds[0] if kinds else "string"
        elif key in ("properties", "items"):
            out[key] = ({name: _gemini_schema(sub) for name, sub in value.items()}
                        if key == "properties" else _gemini_schema(value))
        else:
            out[key] = value
    return out


def _gemini_generation_config(model: str, max_tokens: int, attempt: int = 0) -> dict:
    """On Gemini 2.5 models the model's internal "thinking" tokens are deducted
    from maxOutputTokens, so the old fixed 512 could be used up before the
    function-call arguments were finished, ending in finishReason
    MALFORMED_FUNCTION_CALL (GH#51). Cap thinking and leave real headroom.
    2.5 Flash can turn thinking off (budget 0); 2.5 Pro cannot (minimum 128).
    Other models get no thinkingConfig -- some reject it -- and a larger cap."""
    config = {"maxOutputTokens": max(max_tokens, 2048) * (4 if attempt else 1)}
    if model.startswith("gemini-2.5"):
        config["thinkingConfig"] = {"thinkingBudget": 128 if "pro" in model else 0}
    else:
        config["maxOutputTokens"] = max(config["maxOutputTokens"], 4096)
    return config


async def _call_gemini(model: str, system: str, user_message: str, tool_name: str, tool_description: str, schema: dict, max_tokens: int) -> dict:
    api_key = get_gemini_api_key()
    if not api_key:
        raise ValueError("Gemini API key not configured")

    last_reason = None
    for attempt in range(2):
        body = {
            "system_instruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user_message}]}],
            "tools": [{"function_declarations": [
                {"name": tool_name, "description": tool_description, "parameters": _gemini_schema(schema)},
            ]}],
            "tool_config": {"function_calling_config": {"mode": "ANY", "allowed_function_names": [tool_name]}},
            "generationConfig": _gemini_generation_config(model, max_tokens, attempt),
        }
        async with httpx.AsyncClient(timeout=60.0) as client:
            # Gemini answers 503 "high demand" in short bursts (seen live,
            # GH#51 testing): a couple of spaced retries rides those out
            # instead of failing the reviewer's click. 429 is a quota error and
            # is not retried.
            for delay in (*_GEMINI_TRANSIENT_RETRY_DELAYS, None):
                r = await client.post(
                    f"{_GEMINI_API_BASE}/{model}:generateContent",
                    params={"key": api_key},
                    json=body,
                )
                if r.status_code in (500, 502, 503, 504) and delay is not None:
                    await asyncio.sleep(delay)
                    continue
                break
            r.raise_for_status()
            data = r.json()

        candidate = (data.get("candidates") or [{}])[0]
        for part in (candidate.get("content") or {}).get("parts") or []:
            call = part.get("functionCall")
            if call and call.get("name") == tool_name:
                return call.get("args") or {}
        last_reason = candidate.get("finishReason") or (data.get("promptFeedback") or {}).get("blockReason")
        if last_reason not in ("MAX_TOKENS", "MALFORMED_FUNCTION_CALL"):
            break  # a refusal/safety stop won't improve with more tokens; retry only truncation
    raise ValueError(
        "Gemini did not return a structured response"
        + (f" (finishReason: {last_reason})" if last_reason else "")
    )


_PROVIDER_CALLERS = {
    "anthropic": _call_anthropic,
    "openai": _call_openai,
    "gemini": _call_gemini,
}


async def _call_ai(system: str, user_message: str, tool_name: str, tool_description: str, schema: dict, max_tokens: int = 1024) -> dict:
    provider = get_ai_provider()
    caller = _PROVIDER_CALLERS[provider]
    model = get_ai_model()
    try:
        return await caller(model, system, user_message, tool_name, tool_description, schema, max_tokens)
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text[:300]
        raise ValueError(f"{provider} request failed ({exc.response.status_code}): {detail}") from exc


async def suggest_category_rule(description: str, content_type: str) -> dict:
    system = (
        "You translate a plain-English description of a movie/TV category into a structured "
        "filter rule for a VOD catalog manager. Only use the fields and operators provided -- "
        "the rule engine has no other capabilities (no keyword/plot/mood matching, no cast "
        "matching). If the description can't be fully captured by these fields, do your best "
        "partial approximation and keep the proposed name honest about what you could actually "
        "express, rather than claiming to match something the rule can't.\n\n"
        "Two field misuses have caused real bad rules in production and must be avoided:\n"
        "1. `genre` holds only real content-genre values as tagged by the catalog's metadata "
        "source (examples: Horror, Comedy, Family, Animation, Documentary, Action, Drama) -- it "
        "is NEVER a holiday, season, or theme like \"Halloween\" or \"Christmas\". A holiday/theme "
        "belongs in a `name` (or `provider_category`) contains condition instead -- e.g. for "
        "Halloween content, propose name contains \"halloween\", not genre contains \"halloween\".\n"
        "2. `is_adult` flags literal pornographic/XXX content ONLY -- it says nothing about "
        "violence, language, or general age-appropriateness, and must never be used to imply "
        "\"kid-safe\" or \"family-friendly\". For that, use `content_rating` instead (the real "
        "MPAA/TV rating from TMDB): for movies, values are G/PG/PG-13/R/NC-17; for TV shows, "
        "TV-Y/TV-Y7/TV-G/TV-PG/TV-14/TV-MA. A genuinely kid-safe rule should combine an `any` "
        "match of `content_rating equals G`, `content_rating equals PG` (movies) or "
        "`content_rating equals TV-Y`, `TV-Y7`, `TV-G` (TV), not a guess at genre alone -- genre "
        "is a reasonable secondary signal (e.g. Family/Animation) but content_rating is the field "
        "that actually answers the age-appropriateness question. Be aware content_rating is null "
        "for anything never TMDB-matched or never re-enriched since this field was added, so a "
        "rating-based rule will under-match on an incompletely enriched catalog -- mention this "
        "plainly in the proposed name rather than implying complete coverage."
    )
    user_message = (
        f"Content type: {content_type}\n"
        f"Description: {description}\n\n"
        "Available fields: name, genre (real genre tags only, see system note), year, country "
        "(also holds spoken language), director, is_adult (XXX/porn flag only, see system note), "
        "content_rating (real MPAA/TV rating, see system note -- use this for any kid-safe/"
        "family/age-appropriateness request), provider_category (the source provider's own raw "
        "category label, sometimes holiday/theme-grouped -- e.g. \"Halloween\", \"Christmas "
        "Movies\"). Available ops: contains, starts_with, equals, gte, lte (gte/lte only make "
        "sense for year). Propose a rule."
    )
    return await _call_ai(
        system, user_message, "propose_rule",
        "Propose a structured category filter rule matching the given description.",
        _RULE_SCHEMA,
    )


def _candidate_summary(row: dict) -> str:
    parts = [row.get("name") or "?"]
    if row.get("year"):
        parts.append(f"({row['year']})")
    if row.get("genre"):
        parts.append(f"-- genre: {row['genre']}")
    if row.get("description"):
        parts.append(f"-- {row['description'][:200]}")
    return " ".join(parts)


async def evaluate_candidates_for_category(
    description: str, content_type: str, candidates: list[dict], raise_if_all_failed: bool = False,
) -> list[int]:
    """candidates: pool rows (movies or series), each with at least id/name/
    year/genre/description. Returns the subset of ids Claude judged as
    fitting the description. Batches _AI_EVAL_BATCH_SIZE at a time -- keeps
    each prompt small and each call's failure blast radius small (one bad
    batch doesn't lose judgments on the rest of the candidate set).

    raise_if_all_failed: a failed batch is skipped, so when EVERY batch fails
    (bad/unsupported model, bad key, quota) the old result -- an empty list --
    was indistinguishable from "nothing fit". The interactive route asks for
    that case to raise so the reviewer sees the real error; the scheduler
    leaves it off so a persistent failure isn't retried every poll tick."""
    matched_ids: list[int] = []
    batches = failures = 0
    first_error: Exception | None = None
    system = (
        f"You judge whether {'movies' if content_type == 'movie' else 'TV shows'} fit a described "
        "category, based only on the title/year/genre/synopsis given -- you have no other "
        "information about the actual content. Be conservative: only include a title if it's a "
        "clear, confident fit; when genuinely unsure, leave it out."
    )
    for i in range(0, len(candidates), _AI_EVAL_BATCH_SIZE):
        batches += 1
        batch = candidates[i:i + _AI_EVAL_BATCH_SIZE]
        listing = "\n".join(f"{j}: {_candidate_summary(c)}" for j, c in enumerate(batch))
        user_message = f"Category description: {description}\n\nTitles:\n{listing}\n\nWhich numbered titles fit?"
        try:
            result = await _call_ai(
                system, user_message, "report_matches",
                "Report which numbered titles fit the described category.",
                _BATCH_SCHEMA, max_tokens=512,
            )
        except Exception as exc:
            logger.warning("[ai_assist] batch %d-%d failed, skipping: %s", i, i + len(batch), exc)
            failures += 1
            first_error = first_error or exc
            continue
        for idx in result.get("matches", []):
            if isinstance(idx, int) and 0 <= idx < len(batch):
                matched_ids.append(batch[idx]["id"])
    if raise_if_all_failed and batches and failures == batches and first_error is not None:
        raise ValueError(str(first_error))
    return matched_ids


async def suggest_year_review_match(item_name: str, provider_category_name: str | None, content_type: str, candidates: list[dict]) -> dict:
    """candidates: the same suggestion dicts already built by
    tmdb_sync.search_title (name, year, overview, cast, season_count,
    episode_count, vote_average). Returns a recommended pick for the
    reviewer to consider -- never applied automatically."""
    system = (
        "You help disambiguate an item in a VOD catalog against TMDB search results, when its "
        "own metadata doesn't include a year. Pick the candidate that's most likely the correct "
        "match, or say none are confident matches."
    )
    listing = "\n".join(
        f"{i}: {c.get('name')} ({c.get('year') or '?'}) -- {(c.get('overview') or '')[:200]}"
        + (f" -- cast: {', '.join(c.get('cast') or [])}" if c.get("cast") else "")
        for i, c in enumerate(candidates)
    )
    user_message = (
        f"Item name in catalog: {item_name}\n"
        + (f"Provider's own category for it: {provider_category_name}\n" if provider_category_name else "")
        + f"Content type: {content_type}\n\nTMDB candidates:\n{listing}\n\nWhich is the correct match?"
    )
    return await _call_ai(
        system, user_message, "report_match",
        "Report the best matching candidate, or none.",
        _YEAR_MATCH_SCHEMA, max_tokens=512,
    )


async def verify_duplicate_group(content_type: str, items: list[dict]) -> dict:
    """items: pool rows from one Duplicate Finder candidate group (each with
    at least name/year/genre/description) that already share a normalized
    name and a close-enough year (see vod_db.find_duplicate_groups) -- that's
    a real signal but not proof, since two genuinely different real titles
    can share both (a remake, a same-named film in different years' release
    windows, an obscure title collision). Judges from the same plot/genre
    text a human reviewer would look at, never anything the group detection
    itself didn't already have. A group whose members ALL already share one
    confirmed tmdb_id skips this call entirely -- that's already a stronger
    signal than an AI guess (see vod_bulk_ai_service.py)."""
    system = (
        f"You judge whether two or more {'movies' if content_type == 'movie' else 'TV shows'} in a catalog, "
        "already matched on name and release year, are genuinely the SAME real title (a true duplicate to "
        "merge) or a coincidental collision (different real content that happens to share a name/year -- a "
        "remake, an unrelated title, a franchise entry). Be conservative: only say they're the same when the "
        "available plot/genre details are consistent with that; if there's not enough information to tell, "
        "say so with low confidence rather than guessing."
    )
    listing = "\n".join(f"{i + 1}. {_candidate_summary(item)}" for i, item in enumerate(items))
    user_message = f"Content type: {content_type}\n\nCandidates in this group:\n{listing}\n\nAre these the same real title?"
    return await _call_ai(
        system, user_message, "report_duplicate_verdict",
        "Report whether these candidates are genuinely the same title.",
        _DUPLICATE_VERIFY_SCHEMA, max_tokens=512,
    )
