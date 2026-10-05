import vod_db
import vod_importer


def test_country_suffixes_are_classified_for_language_filter():
    assert vod_db._source_language("#BringBackAlice (PL)") == "PL"
    assert vod_db._source_language("1899 (2022) (DE)") == "DE"
    assert vod_db._source_language("Alba (2021) (ES)") == "ES"
    assert vod_db._source_language("A show (2024) (US)") == "EN"


def test_country_suffixes_are_excluded_at_import_when_not_enabled():
    lang = {"enabled_languages": ["EN", "ES"], "exclude_non_latin": False}
    assert vod_importer._should_auto_archive(
        "#BringBackAlice (PL)", lang=lang, raw_name="#BringBackAlice (PL)"
    )
    assert vod_importer._should_auto_archive(
        "1899 (2022) (DE)", lang=lang, raw_name="1899 (2022) (DE)"
    )
    assert not vod_importer._should_auto_archive(
        "Alba (2021) (ES)", lang=lang, raw_name="Alba (2021) (ES)"
    )
