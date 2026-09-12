"""Real title "3 Bed, 2 Bath, 1 Ghost" (2023) is tagged by a provider as
"SC - 3 Bed, 2 Bath, 1 Ghost (2023)", which collides with the known language
code SC (Seychellois Creole) -- the dash-prefix pattern reads it as a
language-tagged title rather than a bare EN title. Found live 2026-09-12
via DB aggregate language counts (2 rows). Same class of bug as the
HI/PK 2014 title collisions -- fixed the same way, via the dash exception
whitelist.
"""

import vod_db


def test_sc_dash_prefix_not_detected_for_real_title():
    assert vod_db._name_prefix_code("SC - 3 Bed, 2 Bath, 1 Ghost (2023)") is None


def test_sc_source_language_is_en():
    assert vod_db._source_language("SC - 3 Bed, 2 Bath, 1 Ghost (2023)") == "EN"


def test_real_sc_language_prefix_still_detected():
    assert vod_db._name_prefix_code("SC - Some Seychellois Creole Title") == "SC"
