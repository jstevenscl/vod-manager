"""Routes for the Radarr/Sonarr library handoff: settings, connection test, retry, notices."""

import asyncio

import pytest
from fastapi import HTTPException

import arr_client
import config
import notices
import vod_db
import vod_downloader
import vod_routes

from test_arr_handoff import _jobs, _movie


def _save(**kw):
    return asyncio.run(vod_routes.save_handoff_settings(vod_routes.HandoffSettingsRequest(**kw)))


def test_settings_roundtrip_hides_key(db):
    out = _save(radarr_url="http://r:7878/", radarr_api_key="SECRET", radarr_root="/media/Movies",
                radarr_profile_id=6, stall_hours=[2, 6, 12], fail_hours=20)
    got = asyncio.run(vod_routes.get_handoff_settings_route())
    for s in (out, got):
        assert s["radarr_url"] == "http://r:7878" and s["radarr_has_key"] is True
        assert "radarr_api_key" not in s and "SECRET" not in str(s)
        assert s["stall_hours"] == [2, 6, 12] and s["fail_hours"] == 20 and s["radarr_profile_id"] == 6
    _save(radarr_url="http://r:7878")                       # blank key keeps the saved one
    assert config.get_handoff_api_key("radarr") == "SECRET"


@pytest.mark.parametrize("hours,fail", [([8, 4, 16], 24), ([4, 8, 30], 24), ([], 24), ([0, 8, 16], 24)])
def test_bad_stall_hours_rejected(db, hours, fail):
    with pytest.raises(HTTPException) as exc:
        _save(stall_hours=hours, fail_hours=fail)
    assert exc.value.status_code == 400


def test_bad_monitor_rejected(db):
    with pytest.raises(HTTPException) as exc:
        _save(new_show_monitor="all")
    assert exc.value.status_code == 400


# the "as Radarr/Sonarr see them" folder is the finished folder, never the library root
@pytest.mark.parametrize("kw", [
    dict(movies_arr_dir="/media/Movies", radarr_root="/media/Movies"),
    dict(movies_arr_dir="/media/Movies/", radarr_root="/media/Movies"),
    dict(shows_arr_dir="/media/TV", sonarr_root="/media/TV/"),
])
def test_arr_dir_equal_to_root_folder_rejected(db, kw):
    with pytest.raises(HTTPException) as exc:
        _save(**kw)
    assert exc.value.status_code == 400 and "root folder" in exc.value.detail


def test_arr_dir_different_from_root_folder_saved(db):
    out = _save(movies_arr_dir="/media/finished/movies", radarr_root="/media/Movies",
                shows_arr_dir="", sonarr_root="/media/TV")
    assert out["movies_arr_dir"] == "/media/finished/movies"


class _FakeClient:
    seen = []

    def __init__(self, kind, url, key, **kw):
        self.seen.append((kind, url, key))
        self.fail = url == "http://bad"

    def system_status(self):
        if self.fail:
            raise arr_client.ArrError("Radarr: can't connect")
        return {"version": "5.1"}

    def root_folders(self):
        return [{"id": 1, "path": "/media/Movies"}]

    def quality_profiles(self):
        return [{"id": 6, "name": "HD"}]


def test_test_endpoint_returns_profiles(db, monkeypatch):
    monkeypatch.setattr(arr_client, "ArrClient", _FakeClient)
    _save(radarr_url="http://r:7878", radarr_api_key="SAVED")
    out = asyncio.run(vod_routes.test_handoff("radarr", vod_routes.HandoffTestRequest()))
    assert out == {"ok": True, "version": "5.1", "root_folders": [{"id": 1, "path": "/media/Movies"}],
                   "profiles": [{"id": 6, "name": "HD"}]}
    assert _FakeClient.seen[-1] == ("radarr", "http://r:7878", "SAVED")
    # unsaved values from the form win over the saved ones
    asyncio.run(vod_routes.test_handoff("radarr", vod_routes.HandoffTestRequest(url="http://new", api_key="NEW")))
    assert _FakeClient.seen[-1] == ("radarr", "http://new", "NEW")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(vod_routes.test_handoff("radarr", vod_routes.HandoffTestRequest(url="http://bad")))
    assert exc.value.status_code == 400 and "can't connect" in exc.value.detail
    with pytest.raises(HTTPException):
        asyncio.run(vod_routes.test_handoff("lidarr", vod_routes.HandoffTestRequest()))


def test_test_endpoint_needs_url_and_key(db, monkeypatch):
    monkeypatch.setattr(arr_client, "ArrClient", _FakeClient)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(vod_routes.test_handoff("sonarr", vod_routes.HandoffTestRequest()))
    assert exc.value.status_code == 400


def test_ack_notice(db):
    nid = notices.add("import", "warn", "Still waiting for the encoder: X")
    out = asyncio.run(vod_routes.list_notices())
    assert out["count"] == 1 and out["open"][0]["id"] == nid
    assert asyncio.run(vod_routes.ack_notice(nid)) == {"ok": True}
    assert asyncio.run(vod_routes.list_notices()) == {"open": [], "count": 0}
    with pytest.raises(HTTPException) as exc:
        asyncio.run(vod_routes.ack_notice(nid))                # already acknowledged
    assert exc.value.status_code == 404


def _handoff_job(db, tmp_path, status):
    config.save_download_settings({"enabled": True, "output_dir": str(tmp_path / "library"), "min_free_gb": 0})
    vod_downloader.enqueue(movie_ids=[_movie(db)])
    jid = _jobs()[-1]["id"]
    vod_db.update_download_job(jid, status="done", final_path="Some Movie (2001)/Some Movie (2001).mp4")
    vod_db.update_import(jid, import_status=status, import_stage=4, import_note="old")
    return jid


def test_status_includes_import_fields(db, tmp_path):
    jid = _handoff_job(db, tmp_path, "failed")
    [job] = [j for j in asyncio.run(vod_routes.get_downloads())["jobs"] if j["id"] == jid]
    assert job["import_status"] == "failed" and job["import_note"] == "old"


def test_retry_import(db, tmp_path):
    jid = _handoff_job(db, tmp_path, "timed_out")
    assert asyncio.run(vod_routes.retry_import(jid)) == {"ok": True}
    job = vod_db.get_download_job(jid)
    assert (job["import_status"], job["import_stage"], job["import_note"]) == ("waiting", 0, None)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(vod_routes.retry_import(jid))              # waiting already: nothing to retry
    assert exc.value.status_code == 404


@pytest.mark.parametrize("status", ["add", "ready", "waiting", "importing"])
def test_clear_keeps_pending_imports(db, tmp_path, status):
    jid = _handoff_job(db, tmp_path, status)
    assert asyncio.run(vod_routes.clear_downloads()) == {"cleared": 0, "kept": 1}
    assert vod_db.get_download_job(jid) is not None


@pytest.mark.parametrize("status", ["imported", "failed", "timed_out", "needs_manual", None])
def test_clear_removes_settled_imports(db, tmp_path, status):
    jid = _handoff_job(db, tmp_path, status)
    assert asyncio.run(vod_routes.clear_downloads()) == {"cleared": 1, "kept": 0}
    assert vod_db.get_download_job(jid) is None
