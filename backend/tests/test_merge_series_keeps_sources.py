"""Merging two series cards must carry the merged-away card's provider
sources onto the survivor -- otherwise the next import no longer recognises
that provider's show and re-creates it as "new" on every run."""


def _two_separate_cards(db):
    first = db.upsert_provider("Provider One", "http://one.invalid", "u", "p")
    second = db.upsert_provider("Provider Two", "http://two.invalid", "u", "p")
    one = {"name": "PCOK - Example Show", "year": 2020, "provider_series_id": "one-1",
           "tmdb_id": None, "provider_category_name": None, "raw_name": "PCOK - Example Show",
           "_has_detail": True}
    two = {"name": "Example Show", "year": 2020, "provider_series_id": "two-1",
           "tmdb_id": None, "provider_category_name": None, "raw_name": "Example Show",
           "_has_detail": True}
    db.bulk_import_series(first, [one])
    db.bulk_import_series(second, [two])
    by_name = {r["name"]: r["id"] for r in db.list_series(limit=10)}
    return first, one, by_name["PCOK - Example Show"], by_name["Example Show"]


def test_merge_series_moves_sources_to_survivor(db):
    _, _, drop_id, keep_id = _two_separate_cards(db)

    db.merge_series(drop_id, keep_id)

    sources = db.list_series_sources(keep_id)
    assert sorted(s["provider_series_id"] for s in sources) == ["one-1", "two-1"]


def test_reimport_after_merge_creates_nothing_new(db):
    first, one, drop_id, keep_id = _two_separate_cards(db)
    db.merge_series(drop_id, keep_id)

    db.bulk_import_series(first, [one])

    assert [r["id"] for r in db.list_series(limit=10)] == [keep_id]
