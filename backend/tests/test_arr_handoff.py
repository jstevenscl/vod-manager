"""Library handoff to Radarr/Sonarr: add-on-queue step, import watcher, stall notices."""

import asyncio

import pytest

import arr_handoff
import config
import vod_db
import vod_downloader
from arr_client import ArrError


class FakeArr:
    """Stands in for ArrClient. `items` maps tmdb id (or title) -> lookup result."""

    def __init__(self, items=None, fail=False):
        self.items = items or {}
        self.fail = fail
        self.calls = []

    def lookup(self, tmdb_id, title, year):
        self.calls.append(("lookup", tmdb_id, title, year))
        if self.fail:
            raise ArrError("Radarr: can't connect")
        return self.items.get(str(tmdb_id) if tmdb_id else title)

    def add(self, item, root, profile_id, monitor):
        self.calls.append(("add", item.get("title"), root, profile_id, monitor))
        return {**item, "id": 900}

    def names(self):
        return [c[0] for c in self.calls]


@pytest.fixture()
def handoff(db, tmp_path, monkeypatch):
    config.save_download_settings({"enabled": True, "output_dir": str(tmp_path / "library"), "min_free_gb": 0})
    config.save_handoff_settings({"radarr_url": "http://r:7878", "radarr_api_key": "RK",
                                  "sonarr_url": "http://s:8989", "sonarr_api_key": "SK",
                                  "radarr_root": "/media/Movies", "radarr_profile_id": 6,
                                  "sonarr_root": "/media/Tv", "sonarr_profile_id": 6})
    fakes = {}
    monkeypatch.setattr(arr_handoff, "client_for", lambda kind: fakes.get(kind))
    return fakes


def _set_tmdb(db, table, row_id, tmdb):
    conn = db._connect()
    conn.execute(f"UPDATE {table} SET tmdb_id=? WHERE id=?", (tmdb, row_id))
    conn.commit()
    conn.close()


def _movie(db, name="Some Movie", year=2001, tmdb="777"):
    pid = db.upsert_provider("p1", "http://prov", "u", "pw")
    mid = db.upsert_movie(name, year)
    if tmdb:
        _set_tmdb(db, "movies", mid, tmdb)
    db.add_movie_source(mid, pid, "100", "mp4")
    return mid


def _series(db, episodes=((1, 1),), tmdb="555"):
    pid = db.upsert_provider("p1", "http://prov", "u", "pw")
    sid = db.upsert_series("Show", 2020)
    if tmdb:
        _set_tmdb(db, "series", sid, tmdb)
    for season, num in episodes:
        db.add_episode_source(db.add_episode(sid, season, num, f"e{season}{num}"), pid, f"{season}{num}", "mp4")
    return sid


def _jobs():
    conn = vod_db._connect()
    rows = [dict(r) for r in conn.execute("SELECT * FROM download_jobs ORDER BY id").fetchall()]
    conn.close()
    return rows


def _resolve():
    asyncio.run(arr_handoff.resolve_pending())
    return _jobs()


# ── add-on-queue (T3) ───────────────────────────────────────────────────────

def test_queue_marks_add_only_when_configured(db, tmp_path):
    config.save_download_settings({"enabled": True, "output_dir": str(tmp_path / "lib"), "min_free_gb": 0})
    vod_downloader.enqueue(movie_ids=[_movie(db)])
    assert _jobs()[0]["import_status"] is None
    config.save_handoff_settings({"radarr_url": "http://r:7878", "radarr_api_key": "RK"})
    vod_downloader.enqueue(movie_ids=[_movie(db, name="Other", tmdb="778")])
    assert _jobs()[1]["import_status"] == "add"


def test_existing_title_not_re_added(db, handoff):
    handoff["movie"] = fake = FakeArr({"777": {"title": "Some Movie", "id": 42}})
    vod_downloader.enqueue(movie_ids=[_movie(db)])
    job = _resolve()[0]
    assert "add" not in fake.names()
    assert job["arr_id"] == 42 and job["import_status"] == "ready"


def test_missing_movie_added_unmonitored(db, handoff):
    handoff["movie"] = fake = FakeArr({"777": {"title": "Some Movie"}})
    vod_downloader.enqueue(movie_ids=[_movie(db)])
    job = _resolve()[0]
    assert ("add", "Some Movie", "/media/Movies", 6, "unmonitored") in fake.calls
    assert job["arr_id"] == 900 and job["import_status"] == "ready"


def test_missing_show_added_future_only(db, handoff):
    handoff["episode"] = fake = FakeArr({"555": {"title": "Show"}})
    vod_downloader.enqueue(series_ids=[_series(db)])
    _resolve()
    assert ("add", "Show", "/media/Tv", 6, "future") in fake.calls


def test_add_step_groups_episodes_by_series(db, handoff):
    handoff["episode"] = fake = FakeArr({"555": {"title": "Show"}})
    vod_downloader.enqueue(series_ids=[_series(db, episodes=((1, 1), (1, 2), (1, 3)))])
    jobs = _resolve()
    assert fake.names().count("lookup") == 1 and fake.names().count("add") == 1
    assert [j["import_status"] for j in jobs] == ["ready"] * 3
    assert {j["arr_id"] for j in jobs} == {900}


def test_no_tmdb_ambiguous_title_needs_manual(db, handoff):
    handoff["movie"] = fake = FakeArr({})          # title lookup finds no single match
    vod_downloader.enqueue(movie_ids=[_movie(db, tmdb=None)])
    job = _resolve()[0]
    assert fake.calls[0] == ("lookup", None, "Some Movie", 2001)
    assert job["import_status"] == "needs_manual"
    assert job["import_note"] == "Add this title to Radarr/Sonarr manually"


def test_arr_unreachable_during_add_retries_next_tick(db, handoff):
    handoff["movie"] = FakeArr(fail=True)
    vod_downloader.enqueue(movie_ids=[_movie(db)])
    job = _resolve()[0]
    assert job["import_status"] == "add" and "can't connect" in job["import_note"]
    handoff["movie"] = FakeArr({"777": {"title": "Some Movie", "id": 42}})
    job = _resolve()[0]
    assert job["import_status"] == "ready" and job["import_note"] is None


# ── import watcher + stall stages (T4) ──────────────────────────────────────

import notices  # noqa: E402

T0 = 1_000_000.0


class WatchArr(FakeArr):
    def __init__(self, items=None, fail=False, cmd_status="completed", has_file=True, rejection=None):
        super().__init__({"777": {"title": "Some Movie", "id": 42}} if items is None else items, fail)
        self.cmd_status, self.file, self.rejection = cmd_status, has_file, rejection

    def import_path(self, path, arr_id=None, season=None, episode=None):
        self.calls.append(("import", path))
        if self.fail:
            raise ArrError("Radarr: can't connect")
        return 55

    def command(self, cmd_id):
        self.calls.append(("command", cmd_id))
        if self.fail:
            raise ArrError("Radarr: can't connect")
        return {"id": cmd_id, "status": self.cmd_status}

    def has_file(self, arr_id, season=None, episode=None):
        self.calls.append(("has_file", arr_id, season, episode))
        return self.file

    def rejection_for(self, path):
        return self.rejection


@pytest.fixture()
def watch(handoff, tmp_path):
    finished = tmp_path / "finished" / "radarr"
    shows_finished = tmp_path / "finished" / "tv"
    config.save_handoff_settings({"movies_finished_dir": str(finished), "movies_arr_dir": "/media/finished/radarr",
                                  "shows_finished_dir": str(shows_finished)})
    return {"fakes": handoff, "finished": finished, "shows_finished": shows_finished,
            "staging": tmp_path / "library" / "Movies"}


def _done_movie_job(db, status="ready", final="Some Movie (2001)/Some Movie (2001).mp4", since=T0):
    vod_downloader.enqueue(movie_ids=[_movie(db)])
    jid = _jobs()[-1]["id"]
    vod_db.update_download_job(jid, status="done", final_path=final)
    vod_db.update_import(jid, import_status=status, arr_id=42 if status == "ready" else None)
    vod_db.mark_import_waiting(jid)
    vod_db.update_import(jid, import_since=since)
    return jid


def _put(folder, name, data=b"x" * 10):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_bytes(data)
    return folder / name


def _tick(now):
    asyncio.run(arr_handoff.tick(now))


def _job(jid):
    return vod_db.get_download_job(jid)


def test_done_marks_waiting_only_for_handoff_jobs(db, tmp_path):
    config.save_download_settings({"enabled": True, "output_dir": str(tmp_path / "library"), "min_free_gb": 0})
    vod_downloader.enqueue(movie_ids=[_movie(db)])
    plain = _jobs()[0]["id"]
    vod_db.mark_import_waiting(plain)
    assert _job(plain)["import_status"] is None
    # the real done path: the file is already in the library folder
    config.save_handoff_settings({"radarr_url": "http://r:7878", "radarr_api_key": "RK"})
    vod_downloader.enqueue(movie_ids=[_movie(db, name="Other", year=2002, tmdb="778")])
    jid = _jobs()[1]["id"]
    _put(tmp_path / "library" / "Movies" / "Other (2002) [tmdbid-778]", "Other (2002).mp4")
    asyncio.run(vod_downloader.run_job(jid))
    job = _job(jid)
    assert job["status"] == "done" and job["import_status"] == "waiting" and job["import_since"]


def test_finds_file_with_changed_extension(db, watch):
    jid = _done_movie_job(db)
    _put(watch["finished"] / "Some Movie (2001)", "Some Movie (2001).MKV")
    _put(watch["finished"] / "Some Movie (2001)", "Some Movie (2001).nfo")
    found = arr_handoff.finished_path_for(_job(jid), config.get_handoff_settings())
    assert found and found.name == "Some Movie (2001).MKV"


def test_waits_for_size_stable_60s(db, watch):
    watch["fakes"]["movie"] = fake = WatchArr()
    jid = _done_movie_job(db)
    _put(watch["finished"] / "Some Movie (2001)", "Some Movie (2001).mkv")
    _tick(T0 + 10)
    assert "import" not in fake.names() and _job(jid)["import_seen_size"] == 10
    _tick(T0 + 40)
    assert "import" not in fake.names()
    _tick(T0 + 71)
    assert ("import", "/media/finished/radarr/Some Movie (2001)") in fake.calls
    job = _job(jid)
    assert job["import_status"] == "importing" and job["import_cmd_id"] == 55


def test_episode_import_sends_the_file(db, watch):
    watch["fakes"]["episode"] = fake = WatchArr({"555": {"title": "Show", "id": 9}})
    vod_downloader.enqueue(series_ids=[_series(db)])
    jid = _jobs()[-1]["id"]
    vod_db.update_download_job(jid, status="done", final_path="Show (2020)/Season 01/Show - S01E01.mp4")
    vod_db.update_import(jid, import_status="ready", arr_id=9)
    vod_db.mark_import_waiting(jid)
    f = _put(watch["shows_finished"] / "Show (2020)" / "Season 01", "Show - S01E01.mkv")
    _tick(T0)
    _tick(T0 + 61)
    assert ("import", str(f)) in fake.calls     # no arr dir set: same path


def _importing(db, watch, **fake_kw):
    watch["fakes"]["movie"] = fake = WatchArr(**fake_kw)
    jid = _done_movie_job(db)
    vod_db.update_import(jid, import_status="importing", import_cmd_id=55, import_cmd_at=T0)
    _put(watch["finished"] / "Some Movie (2001)", "Some Movie (2001).mkv")
    return jid, fake


def test_importing_completed_with_file_becomes_imported(db, watch):
    jid, fake = _importing(db, watch)
    _tick(T0 + 30)
    assert ("has_file", 42, None, None) in fake.calls
    assert _job(jid)["import_status"] == "imported" and notices.count_open() == 0


def test_importing_completed_without_file_fails_with_rejection(db, watch):
    jid, _ = _importing(db, watch, has_file=False, rejection="Custom Format score 0 below minimum 1")
    _tick(T0 + 30)
    job = _job(jid)
    assert job["import_status"] == "failed" and "below minimum" in job["import_note"]
    [n] = notices.list_open()
    assert n["severity"] == "error" and n["title"] == "Import failed: Some Movie (2001)" and n["job_id"] == jid


def test_importing_still_running_after_15_min_times_out(db, watch):
    jid, _ = _importing(db, watch, cmd_status="started")
    _tick(T0 + 60)
    assert _job(jid)["import_status"] == "importing"
    _tick(T0 + 15 * 60 + 1)
    assert _job(jid)["import_status"] == "timed_out" and notices.count_open() == 1


def test_arr_unreachable_keeps_waiting(db, watch):
    watch["fakes"]["movie"] = WatchArr(fail=True)
    jid = _done_movie_job(db)
    _put(watch["finished"] / "Some Movie (2001)", "Some Movie (2001).mkv")
    _tick(T0)
    _tick(T0 + 61)
    job = _job(jid)
    assert job["import_status"] == "waiting" and "can't connect" in job["import_note"]


def test_stage_notice_fires_once_across_ticks(db, watch):
    watch["fakes"]["movie"] = WatchArr()
    jid = _done_movie_job(db)
    _tick(T0 + 4.1 * 3600)
    _tick(T0 + 4.2 * 3600)
    assert notices.count_open() == 1 and _job(jid)["import_stage"] == 1
    [n] = notices.list_open()
    assert n["title"] == "Still waiting for the encoder: Some Movie (2001)" and n["severity"] == "warn"
    _tick(T0 + 8.1 * 3600)
    assert notices.count_open() == 2 and _job(jid)["import_stage"] == 2
    assert _job(jid)["import_status"] == "waiting"


def test_24h_marks_failed_with_cause(db, watch):
    watch["fakes"]["movie"] = WatchArr()
    jid = _done_movie_job(db)
    _put(watch["staging"] / "Some Movie (2001)", "Some Movie (2001).mp4")    # never left the download folder
    _tick(T0 + 24.1 * 3600)
    job = _job(jid)
    assert job["import_status"] == "failed" and job["import_stage"] == 4
    assert job["import_note"] == "The encoder hasn't picked it up or hasn't finished. Check its queue or nodes."
    assert any(n["title"] == "Import failed: Some Movie (2001)" for n in notices.list_open())


def test_cause_when_file_is_nowhere(db, watch):
    jid = _done_movie_job(db)
    assert arr_handoff.likely_cause(_job(jid), config.get_handoff_settings()) == \
        "The encoder may have failed or moved it. Check its error list."


def test_needs_manual_resolves_on_later_lookup(db, watch):
    watch["fakes"]["movie"] = fake = WatchArr(items={})
    jid = _done_movie_job(db, status="needs_manual")
    _put(watch["finished"] / "Some Movie (2001)", "Some Movie (2001).mkv")
    _tick(T0)
    assert _job(jid)["import_status"] == "waiting" and _job(jid)["arr_id"] is None
    fake.items = {"777": {"title": "Some Movie", "id": 42}}     # user added it in Radarr
    _tick(T0 + 30)
    _tick(T0 + 91)
    job = _job(jid)
    assert job["arr_id"] == 42 and job["import_status"] == "importing"
    assert "add" not in fake.names()


def test_retry_resets_stage_and_clock(db, watch, monkeypatch):
    jid = _done_movie_job(db)
    vod_db.update_import(jid, import_status="failed", import_stage=4, import_cmd_id=55, import_seen_at=T0)
    monkeypatch.setattr(arr_handoff.time, "time", lambda: T0 + 99)
    asyncio.run(arr_handoff.retry_import(jid))
    job = _job(jid)
    assert (job["import_status"], job["import_stage"], job["import_cmd_id"], job["import_seen_at"],
            job["import_since"]) == ("waiting", 0, None, None, T0 + 99)


def test_loop_noop_when_unconfigured(db, monkeypatch):
    built = []
    monkeypatch.setattr(arr_handoff, "client_for", lambda kind: built.append(kind))
    _tick(T0)
    assert built == []
