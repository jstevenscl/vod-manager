import vod_importer


def test_cancel_bulk_enrichment_sets_request_flag():
    original_running = vod_importer._ENRICH_PROGRESS["running"]
    original_cancel = vod_importer._ENRICH_CANCEL_REQUESTED
    try:
        vod_importer._ENRICH_PROGRESS["running"] = True
        vod_importer._ENRICH_CANCEL_REQUESTED = False
        assert vod_importer.cancel_bulk_enrichment() is True
        assert vod_importer._ENRICH_CANCEL_REQUESTED is True
    finally:
        vod_importer._ENRICH_PROGRESS["running"] = original_running
        vod_importer._ENRICH_CANCEL_REQUESTED = original_cancel


def test_cancel_bulk_enrichment_is_noop_when_idle():
    original_running = vod_importer._ENRICH_PROGRESS["running"]
    original_cancel = vod_importer._ENRICH_CANCEL_REQUESTED
    try:
        vod_importer._ENRICH_PROGRESS["running"] = False
        vod_importer._ENRICH_CANCEL_REQUESTED = False
        assert vod_importer.cancel_bulk_enrichment() is False
        assert vod_importer._ENRICH_CANCEL_REQUESTED is False
    finally:
        vod_importer._ENRICH_PROGRESS["running"] = original_running
        vod_importer._ENRICH_CANCEL_REQUESTED = original_cancel
