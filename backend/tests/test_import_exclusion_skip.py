"""Import-policy exclusions are applied before catalog persistence."""

import vod_importer


def test_excluded_category_returns_true():
    assert vod_importer._should_exclude_from_import(
        "Some Movie", "Foreign Films", ["Foreign Films"], False, {"enabled_languages": ["EN", "ES"], "exclude_non_latin": False},
    ) is True


def test_non_excluded_category_returns_false():
    assert vod_importer._should_exclude_from_import(
        "Some Movie", "Action Movies", ["Foreign Films"], False, {"enabled_languages": ["EN", "ES"], "exclude_non_latin": False},
    ) is False


def test_uncategorized_excluded_when_flag_set():
    assert vod_importer._should_exclude_from_import(
        "Some Movie", None, [], True, {"enabled_languages": ["EN", "ES"], "exclude_non_latin": False},
    ) is True


def test_uncategorized_not_excluded_when_flag_unset():
    assert vod_importer._should_exclude_from_import(
        "Some Movie", None, [], False, {"enabled_languages": ["EN", "ES"], "exclude_non_latin": False},
    ) is False


def test_language_prefix_exclusion_still_works():
    assert vod_importer._should_exclude_from_import(
        "GR - Some Movie", None, [], False, {"enabled_languages": ["EN", "ES"], "exclude_non_latin": False},
    ) is True
