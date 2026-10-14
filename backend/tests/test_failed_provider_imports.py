"""Catalog status names every active provider whose latest import failed."""


def _run(db, provider_id, name, error=None):
    run_id = db.create_catalog_sync_run(provider_id, name, "xc")
    db.finish_catalog_sync_run(run_id, error=error)


def test_lists_each_providers_latest_failure_newest_first(db):
    a = db.upsert_provider("A", "http://a.example", "u", "p")
    b = db.upsert_provider("B", "http://b.example", "u", "p")
    _run(db, a, "A", "ConnectError")
    _run(db, b, "B", "HTTP 403")

    failed = db.list_failed_provider_imports()

    assert [f["provider_name"] for f in failed] == ["B", "A"]
    assert failed[0]["error"] == "HTTP 403"


def test_a_later_success_clears_the_failure(db):
    a = db.upsert_provider("A", "http://a.example", "u", "p")
    _run(db, a, "A", "ConnectError")
    _run(db, a, "A")
    assert db.list_failed_provider_imports() == []


def test_inactive_providers_are_not_reported(db):
    a = db.upsert_provider("A", "http://a.example", "u", "p")
    _run(db, a, "A", "ConnectError")
    db.set_provider_active(a, False)
    assert db.list_failed_provider_imports() == []
