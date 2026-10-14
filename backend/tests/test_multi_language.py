"""vod_manager-vsz: MULTI-LANG / MULTI-SUB content is its own language bucket
that single-language filters never hide and merge gates never split."""

import vod_db


def test_multi_tags_on_title_or_category_are_detected():
    assert vod_db._source_language("Movie Title [MULTI-SUB]") == "MULTI"
    assert vod_db._source_language("Movie Title", "[MULTI-LANG] Movies") == "MULTI"
    assert vod_db._source_language("Movie Title (multi_sub)") == "MULTI"


def test_an_explicit_language_prefix_still_wins():
    assert vod_db._source_language("DE| Film [MULTI-SUB]") == "DE"
    assert vod_db._source_language("Film", "[FR] Cinema") == "FR"
    assert vod_db._source_language("Plain Title") == "EN"


def test_multi_is_never_filtered_out_by_the_enabled_languages(db, monkeypatch):
    monkeypatch.setattr(vod_db, "get_enabled_languages", lambda: ["DE"])
    clause, params = vod_db._enabled_languages_clause("ms.language")
    # evaluate the clause directly against literal values
    conn = vod_db._connect()
    try:
        def visible(lang):
            return bool(conn.execute(f"SELECT 1 FROM (SELECT ? AS language) ms WHERE {clause}", (lang, *params)).fetchone())
        assert visible("DE") and visible("MULTI")
        assert not visible("EN") and not visible("FR")
    finally:
        conn.close()


def test_multi_overlaps_every_language_for_merge_gates():
    assert vod_db._languages_overlap({"MULTI"}, {"EN"})
    assert vod_db._languages_overlap({"DE"}, {"MULTI", "FR"})
    assert not vod_db._languages_overlap({"DE"}, {"EN"})
    assert vod_db._any_language_enabled({"MULTI"}, {"DE"})
    assert not vod_db._any_language_enabled({"EN"}, {"DE"})
