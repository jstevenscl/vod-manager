"""IR (Iranian/Persian) is a real provider prefix code seen live ("IR -
(500) Days of Summer") but was missing from _KNOWN_LANGUAGE_CODES, so
_name_prefix_code never recognized it and _source_language silently
defaulted those sources to "EN" -- meaning an admin who excludes everything
but EN via Enabled Playback Languages still had IR-tagged sources served,
since they were misclassified as English at the language-computation layer
rather than actually being English. Found live 2026-09-11 while verifying
the enabled_languages gate for a different question. Frontend's
LANGUAGE_CODE_NAMES already lists IR as its own code (distinct from FA/
Persian-Farsi), confirming IR is the real provider tag and not a typo for FA.
"""

import vod_db


def test_ir_dash_prefix_recognized():
    assert vod_db._name_prefix_code("IR - (500) Days of Summer (2009)") == "IR"


def test_ir_source_language_not_en():
    assert vod_db._source_language("IR - (500) Days of Summer (2009)") == "IR"


def test_ir_prefix_stripped_from_display_name():
    assert vod_db._strip_one_lang_prefix("IR - (500) Days of Summer") == "(500) Days of Summer"
