"""Library handoff settings (Radarr/Sonarr) and the download_jobs import columns."""

import json

import config
import vod_db


def test_handoff_defaults_off(db):
    s = config.get_handoff_settings()
    assert config.handoff_enabled("movie") is False and config.handoff_enabled("episode") is False
    assert s["new_show_monitor"] == "future" and s["new_movie_monitor"] == "unmonitored"
    assert s["stall_hours"] == [4, 8, 16] and s["fail_hours"] == 24
    assert s["radarr_has_key"] is False


def test_api_key_encrypted_and_hidden(db):
    config.save_handoff_settings({"radarr_url": "http://r:7878", "radarr_api_key": "SECRET123"})
    raw = json.loads(config.CONFIG_FILE.read_text())
    assert "SECRET123" not in json.dumps(raw)
    s = config.get_handoff_settings()
    assert s["radarr_has_key"] is True and "radarr_api_key" not in s
    assert "SECRET123" not in json.dumps(s)
    assert config.get_handoff_api_key("radarr") == "SECRET123"
    assert config.handoff_enabled("movie") is True and config.handoff_enabled("episode") is False


def test_blank_key_keeps_existing_and_dash_clears(db):
    config.save_handoff_settings({"sonarr_url": "http://s:8989", "sonarr_api_key": "K1"})
    config.save_handoff_settings({"sonarr_url": "http://s:8989", "sonarr_api_key": ""})
    assert config.get_handoff_api_key("sonarr") == "K1"
    config.save_handoff_settings({"sonarr_api_key": "-"})
    assert config.get_handoff_api_key("sonarr") is None
    assert config.handoff_enabled("episode") is False


def test_bad_monitor_values_fall_back(db):
    config.save_handoff_settings({"new_show_monitor": "all", "new_movie_monitor": "x"})
    s = config.get_handoff_settings()
    assert s["new_show_monitor"] == "future" and s["new_movie_monitor"] == "unmonitored"


def test_import_columns_exist(db):
    conn = vod_db._connect()
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(download_jobs)")}
    conn.close()
    assert {"import_status", "import_stage", "import_note", "arr_id", "import_cmd_id", "import_cmd_at",
            "import_seen_size", "import_seen_at", "import_since"} <= cols


def test_update_import_rejects_other_fields(db):
    jid = vod_db.enqueue_download("movie", 1, "X (2000)", "original")
    vod_db.update_import(jid, import_status="add", arr_id=5)
    job = vod_db.get_download_job(jid)
    assert job["import_status"] == "add" and job["arr_id"] == 5 and job["import_stage"] == 0
    import pytest
    with pytest.raises(ValueError):
        vod_db.update_import(jid, status="done")
    assert vod_db.list_import_jobs(("add",))[0]["id"] == jid
