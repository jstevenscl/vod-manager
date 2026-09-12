"""Real movie titles "HI" (2014, Telugu/Bollywood horror-comedy) and "PK"
(2014, the Aamir Khan Bollywood comedy) collide with the known language
codes HI (Hindi) and PK (Pakistani) -- WarpTV tags them as "HI - 2014" and
"PK - 2014", which the dash-prefix pattern reads as a language-tagged title
rather than a bare title with a year suffix. Found live 2026-09-11 during
WarpTV import verification: both were misclassified into movie_sources with
language='HI'/'PK' instead of 'EN', so an admin with only EN enabled would
have (wrongly) had them excluded. Same class of bug as the "IT: Chapter Two"
colon exception -- fixed the same way, via the dash exception whitelist.
"""

import vod_db


def test_hi_dash_prefix_not_detected_for_real_title():
    assert vod_db._name_prefix_code("HI - 2014") is None


def test_pk_dash_prefix_not_detected_for_real_title():
    assert vod_db._name_prefix_code("PK - 2014") is None


def test_hi_source_language_is_en():
    assert vod_db._source_language("HI - 2014") == "EN"


def test_pk_source_language_is_en():
    assert vod_db._source_language("PK - 2014") == "EN"


def test_real_hi_language_prefix_still_detected():
    assert vod_db._name_prefix_code("HI - Kabhi Khushi Kabhie Gham") == "HI"


def test_real_pk_language_prefix_still_detected():
    assert vod_db._name_prefix_code("PK - Some Pakistani Drama") == "PK"
