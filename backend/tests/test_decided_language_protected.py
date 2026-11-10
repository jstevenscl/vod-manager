# KNM: 2026-11-05 - an approved probe/manual language decision must survive
# re-sync, enrichment re-upserts and the recompute tool; otherwise a hidden
# unwanted feed silently becomes playable again.
import vod_db as _vdb  # noqa: F401  (db fixture provides the module)


def _approve(db, table, source_id, language):
    conn = db._connect()
    try:
        conn.execute(f"UPDATE {table} SET language=?, language_probe_decision='approved' WHERE id=?", (language, source_id))
        conn.commit()
    finally:
        conn.close()


def _lang(db, table, source_id):
    conn = db._connect()
    try:
        return conn.execute(f"SELECT language FROM {table} WHERE id=?", (source_id,)).fetchone()["language"]
    finally:
        conn.close()


def _movie_source_id(db, stream_id):
    conn = db._connect()
    try:
        return conn.execute("SELECT id FROM movie_sources WHERE provider_stream_id=?", (stream_id,)).fetchone()["id"]
    finally:
        conn.close()


def test_recompute_skips_approved_source(db):
    pid = db.upsert_provider("prov-rc", "http://example.com", "u", "p")
    db.bulk_import_movies(pid, [{"name": "Kept", "year": 2020, "provider_stream_id": "k1",
                                 "container_extension": "mp4", "raw_name": "Kept"}])
    sid = _movie_source_id(db, "k1")
    _approve(db, "movie_sources", sid, "JA")

    assert db.apply_language_recompute() == 0
    assert _lang(db, "movie_sources", sid) == "JA"
    assert "movie_sources" not in db.language_recompute_dry_run_report()


def test_recompute_still_fixes_undecided_source(db):
    pid = db.upsert_provider("prov-rc2", "http://example.com", "u", "p")
    db.bulk_import_movies(pid, [{"name": "Fix", "year": 2020, "provider_stream_id": "f1",
                                 "container_extension": "mp4", "raw_name": "IR - Fix"}])
    sid = _movie_source_id(db, "f1")
    conn = db._connect()
    conn.execute("UPDATE movie_sources SET language='EN' WHERE id=?", (sid,))
    conn.commit(); conn.close()

    assert db.apply_language_recompute() == 1
    assert _lang(db, "movie_sources", sid) == "IR"


def test_xc_resync_keeps_approved_movie_language(db):
    pid = db.upsert_provider("prov-sync", "http://example.com", "u", "p")
    item = {"name": "Synced", "year": 2020, "provider_stream_id": "s1",
            "container_extension": "mp4", "raw_name": "Synced"}
    db.bulk_import_movies(pid, [item])
    sid = _movie_source_id(db, "s1")
    _approve(db, "movie_sources", sid, "ES")

    db.bulk_import_movies(pid, [dict(item, raw_name="Synced (renamed)", container_extension="mkv")])

    assert _lang(db, "movie_sources", sid) == "ES"


def test_plex_resync_keeps_approved_movie_language(db):
    pid = db.upsert_provider("prov-plex", "", "", "", provider_type="plex")
    item = {"name": "Plexed", "year": 2020, "provider_stream_id": "px1",
            "container_extension": "mkv", "raw_name": "Plexed"}
    db.bulk_import_plex_movies(pid, [item])
    sid = _movie_source_id(db, "px1")
    _approve(db, "movie_sources", sid, "FR")

    db.bulk_import_plex_movies(pid, [item])

    assert _lang(db, "movie_sources", sid) == "FR"


def test_episode_reupsert_keeps_approved_language(db):
    pid = db.upsert_provider("prov-ep", "http://example.com", "u", "p")
    series_id = db.upsert_series("Kept Show", 2010)
    eid = db.add_episode(series_id, 1, 1, "Pilot")
    src = db.add_episode_source(eid, pid, "e1", "mp4", raw_name="Kept Show S01E01")
    _approve(db, "episode_sources", src, "ES")

    db.add_episode_source(eid, pid, "e1", "mp4", raw_name="Kept Show S01E01")

    assert _lang(db, "episode_sources", src) == "ES"


def test_episode_reupsert_updates_undecided_language(db):
    pid = db.upsert_provider("prov-ep2", "http://example.com", "u", "p")
    series_id = db.upsert_series("Moving Show", 2010)
    eid = db.add_episode(series_id, 1, 1, "Pilot")
    src = db.add_episode_source(eid, pid, "e2", "mp4", raw_name="Moving Show S01E01")

    db.add_episode_source(eid, pid, "e2", "mp4", raw_name="IR - Moving Show S01E01")

    assert _lang(db, "episode_sources", src) == "IR"


# KNM: 2026-11-05 - probe codes (KO, FRE) and label codes (KR, FR) must compare as the same language.
def test_language_aliases_canonicalise_label_and_probe_codes(db):
    assert db.canonical_language("KR") == db.canonical_language("KO") == "KO"
    assert db.canonical_language("FRE") == db.canonical_language("fr") == "FR"
    assert db.canonical_language("JP") == "JA"
    assert db.canonical_language("EN") == "EN"


def test_probe_alias_of_enabled_language_is_kept_and_served(db, tmp_path, monkeypatch):
    import config
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "alias.json")
    config.save_enabled_languages(["EN", "KR"])
    pid = db.upsert_provider("alias-prov", "", "", "", provider_type="library")
    mid = db.upsert_movie("Alias Movie", 2020)
    db.add_movie_source(mid, pid, "alias-1", "mkv")
    sid = _movie_source_id(db, "alias-1")
    conn = db._connect()
    conn.execute("UPDATE movie_sources SET language='EN' WHERE id=?", (sid,))
    conn.commit(); conn.close()
    db.set_source_language_probe("movie", sid, ["KO"], "observed", "embedded_audio_tags", "high")

    result = db.decide_language_mismatch("movie", sid, "approved")

    assert result["action"].startswith("language_updated")
    assert _lang(db, "movie_sources", sid) == "KR"
    assert [r["source_id"] for r in db.list_movie_sources_for_streaming(mid)] == [sid]


def test_probe_maps_three_letter_bibliographic_codes():
    import vod_probe
    for raw, code in {"fre": "FR", "cze": "CS", "hun": "HU", "ind": "ID", "ukr": "UK", "rum": "RO"}.items():
        assert vod_probe._LANGUAGE_CODES[raw] == code


def test_configured_language_present_uses_aliases():
    import vod_probe
    assert vod_probe._configured_present("KR", ["KO", "EN"]) is True
    assert vod_probe._configured_present("FR", ["FRE"]) is True
    assert vod_probe._configured_present("EN", ["JA"]) is False
    assert vod_probe._configured_present(None, ["EN"]) is False
