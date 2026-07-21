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
                    "field": {"type": "string", "enum": ["name", "genre", "year", "country", "language", "director", "is_adult"]},
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


async def _call_gemini(model: str, system: str, user_message: str, tool_name: str, tool_description: str, schema: dict, max_tokens: int) -> dict:
    api_key = get_gemini_api_key()
    if not api_key:
        raise ValueError("Gemini API key not configured")

    # Gemini's function-call schema is JSON Schema minus "additionalProperties"
    # and it's fussier about unknown keys than OpenAI/Anthropic -- strip
    # nothing here since our schemas are already plain enough, but keep this
    # call isolated so a future incompatibility only needs a fix in one place.
    body = {
        "system_instruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user_message}]}],
        "tools": [{"function_declarations": [
            {"name": tool_name, "description": tool_description, "parameters": schema},
        ]}],
        "tool_config": {"function_calling_config": {"mode": "ANY", "allowed_function_names": [tool_name]}},
        "generationConfig": {"maxOutputTokens": max_tokens},
    }
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.post(
            f"{_GEMINI_API_BASE}/{model}:generateContent",
            params={"key": api_key},
            json=body,
        )
        r.raise_for_status()
        data = r.json()

    try:
        parts = data["candidates"][0]["content"]["parts"]
        for part in parts:
            call = part.get("functionCall")
            if call and call.get("name") == tool_name:
                return call["args"]
    except (KeyError, IndexError):
        pass
    raise ValueError("Gemini did not return a structured response")


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
        "express, rather than claiming to match something the rule can't."
    )
    user_message = (
        f"Content type: {content_type}\n"
        f"Description: {description}\n\n"
        "Available fields: name, genre, year, country (also holds spoken language), director, "
        "is_adult. Available ops: contains, starts_with, equals, gte, lte (gte/lte only make "
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


async def evaluate_candidates_for_category(description: str, content_type: str, candidates: list[dict]) -> list[int]:
    """candidates: pool rows (movies or series), each with at least id/name/
    year/genre/description. Returns the subset of ids Claude judged as
    fitting the description. Batches _AI_EVAL_BATCH_SIZE at a time -- keeps
    each prompt small and each call's failure blast radius small (one bad
    batch doesn't lose judgments on the rest of the candidate set)."""
    matched_ids: list[int] = []
    system = (
        f"You judge whether {'movies' if content_type == 'movie' else 'TV shows'} fit a described "
        "category, based only on the title/year/genre/synopsis given -- you have no other "
        "information about the actual content. Be conservative: only include a title if it's a "
        "clear, confident fit; when genuinely unsure, leave it out."
    )
    for i in range(0, len(candidates), _AI_EVAL_BATCH_SIZE):
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
            continue
        for idx in result.get("matches", []):
            if isinstance(idx, int) and 0 <= idx < len(batch):
                matched_ids.append(batch[idx]["id"])
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
