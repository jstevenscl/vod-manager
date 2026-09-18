"""Import-time language/country exclusion must key off the provider's raw,
unmodified title -- not the display name after Title & Metadata Rules have
already run on it. Real bug (found live, credit @Knm): a display rule that
strips a leading language prefix for cosmetic reasons (e.g. "ES - Title" ->
"Title") also erased the only signal _should_auto_archive had to exclude
it, so a display-cleanup rule could silently defeat Import Language
Exclusion for every title it touched. _should_auto_archive's raw_name
param (checked instead of the display name when given) fixes this; these
tests cover the two importer call sites that apply rules -- movies and
series -- plus the direct unit-level check on _should_auto_archive itself.
"""

import asyncio

import vod_importer


def test_should_auto_archive_uses_raw_name_over_stripped_display_name(db):
    # Simulates a "strip ES - " display rule having already run: `name` no
    # longer carries the prefix, but `raw_name` (the provider's own,
    # untouched title) still does.
    assert vod_importer._should_auto_archive(
        "3 dias en Malay (2023)",
        lang={"exclude_prefixes": ["ES"], "exclude_non_latin": False},
        raw_name="ES - 3 dias en Malay (2023)",
    ) is True


def test_should_auto_archive_without_raw_name_falls_back_to_name(db):
    # No raw_name passed -- must keep working exactly as before for any
    # caller that doesn't have a separate raw title (Plex/Emby, whose
    # display name already is the raw provider title).
    assert vod_importer._should_auto_archive(
        "ES - 3 dias en Malay (2023)",
        lang={"exclude_prefixes": ["ES"], "exclude_non_latin": False},
    ) is True


def test_should_auto_archive_unprefixed_title_not_over_excluded(db):
    assert vod_importer._should_auto_archive(
        "3 Days in Malay (2023)",
        lang={"exclude_prefixes": ["ES"], "exclude_non_latin": False},
        raw_name="3 Days in Malay (2023)",
    ) is False


class _FakeClient:
    def __init__(self, streams=None, series=None):
        self._streams = streams or []
        self._series = series or []

    async def get_vod_streams(self):
        return self._streams

    async def get_series(self):
        return self._series


def test_movie_import_excludes_stream_after_display_rule_strips_prefix(db, monkeypatch):
    ES_STRIP_RULE = {"id": 1, "pattern": r"^ES - ", "replacement": "", "is_regex": True}
    monkeypatch.setattr(
        vod_importer.vod_db, "get_active_rules_for_field",
        lambda content_type, field: [ES_STRIP_RULE] if (content_type, field) == ("movie", "name") else [],
    )
    captured = {}
    monkeypatch.setattr(
        vod_importer.vod_db, "bulk_import_movies",
        lambda provider_id, items: captured.setdefault("items", items) or {"movies_created": 0, "movies_matched": 0, "total": 0, "flagged_for_review": 0, "errors": 0},
    )

    client = _FakeClient(streams=[{
        "name": "ES - 3 dias en Malay (2023)", "stream_id": "123",
        "category_id": "5", "container_extension": "mp4",
    }])
    lang = {"exclude_prefixes": ["ES"], "exclude_non_latin": False}

    asyncio.run(vod_importer._import_movies_for_provider(
        client, {"id": 4, "name": "TestProvider"}, provider_id=4,
        category_names={"5": "Movies"}, exclude_categories=[], exclude_uncategorized=False,
        lang=lang, country=[],
    ))

    assert captured["items"][0]["auto_archive"] is True, (
        "ES-prefixed stream should still be flagged for archive via the raw provider "
        "name, even though the display-name rule already stripped its prefix"
    )


def test_series_import_excludes_stream_after_display_rule_strips_prefix(db, monkeypatch):
    ES_STRIP_RULE = {"id": 1, "pattern": r"^ES - ", "replacement": "", "is_regex": True}
    monkeypatch.setattr(
        vod_importer.vod_db, "get_active_rules_for_field",
        lambda content_type, field: [ES_STRIP_RULE] if (content_type, field) == ("series", "name") else [],
    )
    captured = {}
    monkeypatch.setattr(
        vod_importer.vod_db, "bulk_import_series",
        lambda provider_id, items: captured.setdefault("items", items) or {"series_created": 0, "series_matched": 0, "episodes_imported": 0},
    )

    client = _FakeClient(series=[{
        "name": "ES - La Casa (2020)", "series_id": "9",
        "category_id": "5", "year": 2020,
    }])
    lang = {"exclude_prefixes": ["ES"], "exclude_non_latin": False}

    asyncio.run(vod_importer._import_series_for_provider(
        client, {"id": 4, "name": "TestProvider"}, provider_id=4,
        series_category_names={"5": "Series"}, exclude_categories=[], exclude_uncategorized=False,
        lang=lang, country=[],
    ))

    assert captured["items"][0]["auto_archive"] is True, (
        "ES-prefixed series should still be flagged for archive via the raw provider "
        "name, even though the display-name rule already stripped its prefix"
    )
