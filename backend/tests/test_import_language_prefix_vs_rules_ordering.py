"""Regression test for a real bug found live 2026-09-12: ES-tagged movies
kept showing up in the DB despite enabled_playback_languages=['EN'] (and
despite 'ES' also sitting in the now-vestigial import_exclude_language_
prefixes list), while every OTHER excluded language was correctly kept out.

Root cause: _import_movies_for_provider/_import_series_for_provider (see
_should_exclude_from_import in vod_importer.py) ran the language-prefix
exclusion check against `name` AFTER vod_db.apply_rules_to_value had
already stripped the leading "ES - " prefix via the built-in Title &
Metadata Rules (a real DB rule, id=1, strips ~150+ known language-code
prefixes for display purposes). With the prefix gone, _name_prefix_code
defaulted to "EN", which is always enabled, so the item was never excluded
-- even though vod_db._source_language(raw_name) (run later, using the
UNTOUCHED raw name) correctly tagged the stored row's language as 'ES'.
Net effect: excluded-language rows were tagged correctly yet never
actually excluded, and only reproduced for providers/content whose name
carried a language prefix that a metadata rule also stripped.

This was reportedly already discussed and expected to be fixed in a prior
session but never actually landed -- this test exercises the real
_import_movies_for_provider pipeline (not just _should_exclude_from_import
in isolation, which test_import_exclusion_skip.py already covers) with an
active rule that strips the prefix, to prove the ordering bug for real and
guard against it regressing again."""

import asyncio

import pytest

import vod_importer


ES_STRIP_RULE = {
    "id": 1,
    "pattern": r"^ES - ",
    "replacement": "",
    "is_regex": True,
}


class _FakeClient:
    def __init__(self, streams):
        self._streams = streams

    async def get_vod_streams(self):
        return self._streams


@pytest.fixture(autouse=True)
def _enabled_languages_en_only(monkeypatch):
    monkeypatch.setattr(vod_importer.config, "get_enabled_languages", lambda: ["EN"])
    monkeypatch.setattr(
        vod_importer.config, "get_import_language_exclusion",
        lambda: {"exclude_prefixes": [], "exclude_non_latin": False},
    )


def test_language_prefixed_stream_excluded_even_after_metadata_rule_strips_it(monkeypatch):
    """The real-world case: a metadata rule strips "ES - " from the display
    name before the exclusion check ever runs. The item must still be
    excluded based on the provider's raw, untouched name."""
    streams = [
        {
            "name": "ES - 3 dias en Malay (2023)",
            "stream_id": "123",
            "category_id": "5",
            "container_extension": "mp4",
        },
    ]

    def fake_get_active_rules_for_field(content_type, field):
        return [ES_STRIP_RULE] if (content_type, field) == ("movie", "name") else []

    monkeypatch.setattr(vod_importer.vod_db, "get_active_rules_for_field", fake_get_active_rules_for_field)

    captured = {}

    def fake_bulk_import_movies(provider_id, items):
        captured["items"] = items
        return {"inserted": len(items), "updated": 0, "sources_added": 0}

    monkeypatch.setattr(vod_importer.vod_db, "bulk_import_movies", fake_bulk_import_movies)

    client = _FakeClient(streams)
    provider = {"id": 4, "name": "TestProvider"}

    asyncio.run(vod_importer._import_movies_for_provider(
        client, provider, provider_id=4,
        category_names={"5": "ES - PELICULAS"}, exclude_categories=[], exclude_uncategorized=False,
    ))

    assert captured["items"] == [], (
        "ES-prefixed stream should have been excluded via the raw provider name, "
        "not silently imported after the metadata rule stripped its language prefix"
    )


def test_unprefixed_english_stream_still_imported(monkeypatch):
    """Sanity check the fix doesn't over-exclude: plain EN content (no
    prefix, no rule match) must still pass through normally."""
    streams = [
        {
            "name": "3 Days in Malay (2023)",
            "stream_id": "456",
            "category_id": "9",
            "container_extension": "mp4",
        },
    ]

    def fake_get_active_rules_for_field(content_type, field):
        return [ES_STRIP_RULE] if (content_type, field) == ("movie", "name") else []

    monkeypatch.setattr(vod_importer.vod_db, "get_active_rules_for_field", fake_get_active_rules_for_field)

    captured = {}

    def fake_bulk_import_movies(provider_id, items):
        captured["items"] = items
        return {"inserted": len(items), "updated": 0, "sources_added": 0}

    monkeypatch.setattr(vod_importer.vod_db, "bulk_import_movies", fake_bulk_import_movies)

    client = _FakeClient(streams)
    provider = {"id": 4, "name": "TestProvider"}

    asyncio.run(vod_importer._import_movies_for_provider(
        client, provider, provider_id=4,
        category_names={"9": "Movie-Action"}, exclude_categories=[], exclude_uncategorized=False,
    ))

    assert len(captured["items"]) == 1
    assert captured["items"][0]["raw_name"] == "3 Days in Malay (2023)"
