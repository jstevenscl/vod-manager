def test_episode_streaming_sources_exclude_non_english_spanish_language(db):
    provider_id = db.upsert_provider("prov1", "http://example.com", "user", "pass")
    series_id = db.upsert_series("Some Show", 2020)
    episode_id = db.add_episode(series_id, 1, 1, "Pilot")

    db.add_episode_source(episode_id, provider_id, "en-1", raw_name="Pilot")
    db.add_episode_source(episode_id, provider_id, "nl-1", raw_name="NL - Pilot")

    sources = db.list_episode_sources_for_streaming(episode_id)
    provider_stream_ids = {s["provider_stream_id"] for s in sources}

    assert provider_stream_ids == {"en-1"}


def test_episode_streaming_sources_skip_entirely_when_no_english_or_spanish_source(db):
    provider_id = db.upsert_provider("prov1", "http://example.com", "user", "pass")
    series_id = db.upsert_series("Some Foreign Show", 2020)
    episode_id = db.add_episode(series_id, 1, 1, "Pilot")

    db.add_episode_source(episode_id, provider_id, "nl-1", raw_name="NL - Pilot")
    db.add_episode_source(episode_id, provider_id, "gr-1", raw_name="GR - Pilot")

    sources = db.list_episode_sources_for_streaming(episode_id)

    assert sources == []


def test_episode_export_uses_english_source_not_foreign_when_both_exist(db):
    en_provider_id = db.upsert_provider("prov_en", "http://example.com", "user", "pass", priority=0)
    nl_provider_id = db.upsert_provider("prov_nl", "http://example.com", "user", "pass", priority=10)
    series_id = db.upsert_series("Some Show", 2020)
    episode_id = db.add_episode(series_id, 1, 1, "Pilot")

    # Higher-priority provider's source is NL-tagged -- without a language
    # gate, _source_order_by would rank this one first (provider priority
    # wins ties), proving the CTE's language filter (not luck) excludes it.
    db.add_episode_source(episode_id, en_provider_id, "en-1", raw_name="Pilot")
    db.add_episode_source(episode_id, nl_provider_id, "nl-1", raw_name="NL - Pilot")

    row = db.get_episode_export_row(episode_id)

    assert row["provider_stream_id"] == "en-1"
