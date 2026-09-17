"""Series source IDs must remain attached to their established title."""


def _series(provider_series_id: str, name: str, year: int | None) -> dict:
    return {
        "name": name,
        "year": year,
        "provider_series_id": provider_series_id,
        "provider_category_name": "Series",
        "raw_name": name,
    }


def test_secondary_series_source_does_not_create_a_sourceless_shadow(db):
    provider_a = db.upsert_provider("Provider A", "http://a.invalid", "u", "p", provider_type="xc")
    provider_b = db.upsert_provider("Provider B", "http://b.invalid", "u", "p", provider_type="xc")

    db.bulk_import_series(provider_a, [_series("a-1", "Example Show", 2020)])
    db.bulk_import_series(provider_b, [_series("b-1", "Example Show", 2020)])
    db.bulk_import_series(provider_b, [_series("b-1", "Example Show (US)", None)])

    conn = db._connect()
    try:
        rows = conn.execute("SELECT id FROM series").fetchall()
    finally:
        conn.close()
    assert len(rows) == 1
    assert {source["provider_series_id"] for source in db.list_series_sources(rows[0]["id"])} == {"a-1", "b-1"}


def test_orphan_cleanup_uses_actual_sources_not_legacy_provider_pointer(db):
    provider_id = db.upsert_provider("Provider", "http://a.invalid", "u", "p", provider_type="xc")
    conn = db._connect()
    try:
        with conn:
            conn.execute(
                "INSERT INTO series (name, import_provider_id, import_provider_series_id, created_at) VALUES (?,?,?,?)",
                ("Broken Shadow", provider_id, "lost-id", "2026-09-17T00:00:00"),
            )
    finally:
        conn.close()

    assert db.find_orphans()["orphaned_series"]["count"] == 1
    assert db.purge_orphans()["series_deleted"] == 1
