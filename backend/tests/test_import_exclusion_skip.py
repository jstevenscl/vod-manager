"""Follow-up to beads-974: provider-level category/uncategorized exclusion
(providers.import_exclude_categories / import_exclude_uncategorized) and the
global language-exclusion rule (config.get_import_language_exclusion) used to
only mark an excluded item auto_archive=True -- it was still fully imported
and stored, just hidden from review queues (vod_db.bulk_set_review_excluded's
"still fully browsable/playable/categorizable" archive). User direction
(2026-09-10, referencing Dispatcharr's VOD-group "unchecked = not imported"
setting): excluded content should never be stored at all, mirroring how a
real exclusion is meant to work -- "I don't want this content in my library",
not "store it but hide it".

_should_auto_archive is renamed _should_exclude_from_import (same logic,
name now matches its actual role: a caller-side skip decision, not a
post-import archive flag) and is used by every importer (XC/vod_importer.py,
Plex, Emby) to filter items out of movie_items/series_items BEFORE calling
bulk_import_movies/bulk_import_series, instead of tagging them for archive
after the fact.

2026-09-11 follow-up: the language-prefix gate inverted from an explicit
exclude-list membership test (config.get_import_language_exclusion's
exclude_prefixes -- a separate, manually-maintained list that real gaps like
"IR" could silently fall through) to an explicit include-list membership
test against config.get_enabled_languages() (the same "Enabled Playback
Languages" list already used query-time by vod_db._enabled_languages_clause).
Per user direction ("Replace entirely"): exclude_prefixes no longer
participates in this check at all -- any language not currently enabled is
excluded at import. lang["enabled_languages"] replaces lang["exclude_prefixes"]
as the key this function reads; exclude_non_latin and the category-based
exclusion logic are unchanged."""

import vod_importer


def test_excluded_category_returns_true():
    assert vod_importer._should_exclude_from_import(
        "Some Movie", "Foreign Films", ["Foreign Films"], False, {"enabled_languages": ["EN"], "exclude_non_latin": False},
    ) is True


def test_non_excluded_category_returns_false():
    assert vod_importer._should_exclude_from_import(
        "Some Movie", "Action Movies", ["Foreign Films"], False, {"enabled_languages": ["EN"], "exclude_non_latin": False},
    ) is False


def test_uncategorized_excluded_when_flag_set():
    assert vod_importer._should_exclude_from_import(
        "Some Movie", None, [], True, {"enabled_languages": ["EN"], "exclude_non_latin": False},
    ) is True


def test_uncategorized_not_excluded_when_flag_unset():
    assert vod_importer._should_exclude_from_import(
        "Some Movie", None, [], False, {"enabled_languages": ["EN"], "exclude_non_latin": False},
    ) is False


def test_language_not_in_enabled_set_is_excluded():
    """Replaces the old exclude_prefixes-based
    test_language_prefix_exclusion_still_works: GR is excluded simply by
    being absent from enabled_languages, with no separate exclude list
    involved."""
    assert vod_importer._should_exclude_from_import(
        "GR - Some Movie", None, [], False, {"enabled_languages": ["EN"], "exclude_non_latin": False},
    ) is True


def test_language_in_enabled_set_is_not_excluded():
    assert vod_importer._should_exclude_from_import(
        "GR - Some Movie", None, [], False, {"enabled_languages": ["EN", "GR"], "exclude_non_latin": False},
    ) is False


def test_untagged_name_excluded_when_en_not_in_enabled_set():
    """A name with no recognized language prefix defaults to "EN", matching
    vod_db._source_language's identical fallback (2026-09-11 follow-up: an
    admin whose Enabled Playback Languages list doesn't include "EN" must
    actually exclude the untagged titles that list counts as EN, not
    silently exclude nothing)."""
    assert vod_importer._should_exclude_from_import(
        "Some Movie", None, [], False, {"enabled_languages": ["GR"], "exclude_non_latin": False},
    ) is True


def test_untagged_name_not_excluded_when_en_in_enabled_set():
    assert vod_importer._should_exclude_from_import(
        "Some Movie", None, [], False, {"enabled_languages": ["EN"], "exclude_non_latin": False},
    ) is False


def test_previously_excluded_ir_prefix_now_excluded_by_default():
    """IR was a real gap in the old exclude_prefixes list (never added, so
    Iranian-tagged content silently wasn't excluded even though nobody
    enabled it for playback either). The inverted "not in enabled_languages"
    check closes that gap automatically -- IR excludes now unless an admin
    explicitly enables it, with no manual exclude-list maintenance required."""
    assert vod_importer._should_exclude_from_import(
        "IR - Some Movie", None, [], False, {"enabled_languages": ["EN"], "exclude_non_latin": False},
    ) is True
