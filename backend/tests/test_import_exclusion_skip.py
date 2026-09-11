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
after the fact."""

import vod_importer


def test_excluded_category_returns_true():
    assert vod_importer._should_exclude_from_import(
        "Some Movie", "Foreign Films", ["Foreign Films"], False, {"exclude_prefixes": [], "exclude_non_latin": False},
    ) is True


def test_non_excluded_category_returns_false():
    assert vod_importer._should_exclude_from_import(
        "Some Movie", "Action Movies", ["Foreign Films"], False, {"exclude_prefixes": [], "exclude_non_latin": False},
    ) is False


def test_uncategorized_excluded_when_flag_set():
    assert vod_importer._should_exclude_from_import(
        "Some Movie", None, [], True, {"exclude_prefixes": [], "exclude_non_latin": False},
    ) is True


def test_uncategorized_not_excluded_when_flag_unset():
    assert vod_importer._should_exclude_from_import(
        "Some Movie", None, [], False, {"exclude_prefixes": [], "exclude_non_latin": False},
    ) is False


def test_language_prefix_exclusion_still_works():
    assert vod_importer._should_exclude_from_import(
        "GR - Some Movie", None, [], False, {"exclude_prefixes": ["GR"], "exclude_non_latin": False},
    ) is True
