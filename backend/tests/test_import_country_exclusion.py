"""Import Country Exclusion: config get/save, the shared
vod_db._country_suffix_code helper, and _should_auto_archive's new
country-suffix check -- the sibling feature to Import Language Exclusion,
keyed on a title's trailing "(<country code>)" tag instead of a leading
language prefix.
"""

import config
import vod_importer


def test_get_import_country_exclusion_defaults_empty(db):
    assert config.get_import_country_exclusion() == []


def test_save_and_get_import_country_exclusion_roundtrips(db):
    config.save_import_country_exclusion(["nz", " au ", "US"])
    assert config.get_import_country_exclusion() == ["NZ", "AU", "US"]


def test_country_suffix_code_known_codes(db):
    assert db._country_suffix_code("Married at First Sight (NZ)") == "NZ"
    assert db._country_suffix_code("Severance (2022) (US)") == "US"
    assert db._country_suffix_code("Plain Title") is None


def test_country_suffix_code_unknown_code_not_stripped(db):
    # Not on the allowlist -- a real title can legitimately end in
    # "(Something)" that isn't a country tag (a subtitle, an edition tag).
    assert db._country_suffix_code("Some Movie (XX)") is None


def test_should_auto_archive_respects_country_exclusion(db):
    country = ["NZ"]
    assert vod_importer._should_auto_archive(
        "Married at First Sight (NZ)", lang={"exclude_prefixes": [], "exclude_non_latin": False}, country=country,
    ) is True
    assert vod_importer._should_auto_archive(
        "Married at First Sight (AU)", lang={"exclude_prefixes": [], "exclude_non_latin": False}, country=country,
    ) is False


def test_should_auto_archive_country_exclusion_defaults_to_config(db):
    config.save_import_country_exclusion(["NZ"])
    # No explicit country arg -- falls back to reading config, same as lang.
    assert vod_importer._should_auto_archive(
        "Married at First Sight (NZ)", lang={"exclude_prefixes": [], "exclude_non_latin": False},
    ) is True


def test_should_auto_archive_empty_country_exclusion_never_archives(db):
    assert vod_importer._should_auto_archive(
        "Married at First Sight (NZ)", lang={"exclude_prefixes": [], "exclude_non_latin": False}, country=[],
    ) is False


def test_list_all_pool_country_suffixes_counts_real_pool(db):
    db.upsert_movie("Married at First Sight (NZ)", None)
    db.upsert_movie("Married at First Sight (AU)", None)
    db.upsert_movie("Another Show (NZ)", None)
    db.upsert_movie("Untagged Show", None)
    counts = {row["code"]: row["count"] for row in db.list_all_pool_country_suffixes()}
    assert counts == {"NZ": 2, "AU": 1}
