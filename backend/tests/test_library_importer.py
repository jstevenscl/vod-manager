"""End-to-end library_importer behaviour against a real temp folder and the
real DB; only TMDB's network search is stubbed."""

import asyncio
import os

import library_importer
import library_matcher as lm


def _touch(root, rel, size=10):
    path = os.path.join(root, *rel.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"x" * size)
    return path


def _stub_tmdb(monkeypatch):
    table = {
        "the matrix": [lm.Candidate("603", "The Matrix", 1999)],
        "breaking bad": [lm.Candidate("1396", "Breaking Bad", 2008)],
    }

    async def fake_search(query, content_type, year):
        return table.get(query.lower(), [])
    monkeypatch.setattr(lm, "_search", fake_search)


def _mk(db, tmp_path):
    root = str(tmp_path / "media")
    os.makedirs(root)
    pid = db.upsert_provider("nas", root, "", "", provider_type="library")
    return pid, root


def _movie(db, pid):
    conn = db._connect()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT m.id, m.name, m.year, m.tmdb_id, m.description, ms.local_file_path, ms.provider_stream_id "
            "FROM movies m JOIN movie_sources ms ON ms.movie_id=m.id WHERE ms.provider_id=?", (pid,))]
    finally:
        conn.close()


def test_imports_movie_and_episodes_with_local_paths(db, tmp_path, monkeypatch):
    _stub_tmdb(monkeypatch)
    pid, root = _mk(db, tmp_path)
    m_path = _touch(root, "Movies/The.Matrix.1999.1080p.BluRay.x264-GRP.mkv")
    _touch(root, "TV/Breaking Bad (2008)/Season 1/Breaking Bad S01E01.mkv")
    _touch(root, "TV/Breaking Bad (2008)/Season 1/Breaking Bad S01E02.mkv")
    _touch(root, "Movies/Some Home Video (2020).mp4")
    _touch(root, "Movies/notes.txt")

    r = asyncio.run(library_importer.import_library(pid))
    assert r["files_found"] == 4
    assert r["movies_created"] == 2 and r["series_created"] == 1 and r["episodes_imported"] == 2

    movies = {m["name"]: m for m in _movie(db, pid)}
    assert movies["The Matrix"]["tmdb_id"] == "603"           # canonical TMDB title, id stored
    assert movies["The Matrix"]["local_file_path"] == m_path
    assert movies["Some Home Video"]["tmdb_id"] is None       # unmatched still imported, visible


def test_rescan_does_not_blank_enriched_detail(db, tmp_path, monkeypatch):
    _stub_tmdb(monkeypatch)
    pid, root = _mk(db, tmp_path)
    _touch(root, "The.Matrix.1999.mkv")
    asyncio.run(library_importer.import_library(pid))
    movie_id = _movie(db, pid)[0]["id"]
    db.set_movie_enrichment(movie_id, description="A hacker learns the truth.", genre="Sci-Fi")

    asyncio.run(library_importer.import_library(pid))          # rescan
    row = _movie(db, pid)[0]
    assert row["description"] == "A hacker learns the truth."
    assert db.get_movie(movie_id)["genre"] == "Sci-Fi"


def test_removed_file_disappears_but_unmounted_share_is_ignored(db, tmp_path, monkeypatch):
    _stub_tmdb(monkeypatch)
    pid, root = _mk(db, tmp_path)
    a = _touch(root, "The.Matrix.1999.mkv")
    _touch(root, "Some Home Video (2020).mp4")
    asyncio.run(library_importer.import_library(pid))
    assert len(_movie(db, pid)) == 2

    os.remove(a)                                               # one file genuinely deleted
    r = asyncio.run(library_importer.import_library(pid))
    assert [m["name"] for m in _movie(db, pid)] == ["Some Home Video"]
    assert r["removal_cleanup_skipped"] is None

    for f in os.listdir(root):                                 # share "unmounts": empty dir
        os.remove(os.path.join(root, f))
    r = asyncio.run(library_importer.import_library(pid))
    assert r["removal_cleanup_skipped"]
    assert len(_movie(db, pid)) == 1                           # nothing was wiped


def test_missing_root_raises(db, tmp_path):
    pid = db.upsert_provider("nas", str(tmp_path / "nope"), "", "", provider_type="library")
    try:
        asyncio.run(library_importer.import_library(pid))
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "not a directory" in str(exc)


def test_library_files_are_never_deleted_from_disk(db, tmp_path):
    pid, root = _mk(db, tmp_path)
    keep = _touch(root, "Movies/Keep.Me.2001.mkv")
    assert db._delete_file_if_present(keep) is False
    assert os.path.exists(keep)
    # ...but a file outside any library root is still deletable (DVR behavior unchanged)
    other = tmp_path / "dvr.mkv"
    other.write_bytes(b"x")
    assert db._delete_file_if_present(str(other)) is True
    assert not other.exists()


def test_removed_episode_in_surviving_show_is_cleaned(db, tmp_path, monkeypatch):
    _stub_tmdb(monkeypatch)
    pid, root = _mk(db, tmp_path)
    _touch(root, "TV/Breaking Bad (2008)/Season 1/Breaking Bad S01E01.mkv")
    e2 = _touch(root, "TV/Breaking Bad (2008)/Season 1/Breaking Bad S01E02.mkv")
    asyncio.run(library_importer.import_library(pid))
    os.remove(e2)
    r = asyncio.run(library_importer.import_library(pid))
    assert r["removed"]["episode_sources_removed"] == 1
    conn = db._connect()
    try:
        n = conn.execute("SELECT COUNT(*) c FROM episode_sources WHERE provider_id=?", (pid,)).fetchone()["c"]
    finally:
        conn.close()
    assert n == 1


def test_sync_route_rejects_library_provider(db):
    import asyncio as _a
    import pytest
    from fastapi import HTTPException
    import vod_routes
    pid = db.upsert_provider("nas", "/mnt/x", "", "", provider_type="library")
    with pytest.raises(HTTPException) as exc:
        _a.run(vod_routes.sync_provider(pid))
    assert exc.value.status_code == 400


# --- manual fix-match: decisions made in the review UI must stick -----------

def _stub_two_dracula(monkeypatch):
    async def fake_search(query, content_type, year):
        if query.lower() == "dracula":
            return [lm.Candidate("1", "Dracula", 1931), lm.Candidate("138", "Dracula", 1992)]
        return []
    monkeypatch.setattr(lm, "_search", fake_search)


def _row(db, pid, name):
    return next(m for m in _movie(db, pid) if m["name"] == name)


def test_manual_pick_sticks_across_rescans(db, tmp_path, monkeypatch):
    _stub_two_dracula(monkeypatch)
    pid, root = _mk(db, tmp_path)
    _touch(root, "Movies/Dracula.mkv")
    asyncio.run(library_importer.import_library(pid))
    m = _row(db, pid, "Dracula")
    assert m["tmdb_id"] is None                                   # ambiguous -> not guessed

    # what the Missing Artwork UI does when a reviewer picks the 1992 film
    db.resolve_missing_artwork("movie", m["id"], "http://p/1.jpg", "138", "Dracula", 1992)
    decision = db.get_library_match(pid, "Movies/Dracula.mkv", "movie")
    assert (decision["source"], decision["status"], decision["tmdb_id"]) == ("manual", "matched", "138")

    asyncio.run(library_importer.import_library(pid))             # rescan must not undo it
    row = _row(db, pid, "Dracula")
    assert row["tmdb_id"] == "138" and row["year"] == 1992


def test_clearing_a_wrong_match_is_not_reapplied_on_rescan(db, tmp_path, monkeypatch):
    _stub_tmdb(monkeypatch)
    pid, root = _mk(db, tmp_path)
    _touch(root, "The.Matrix.1999.mkv")
    asyncio.run(library_importer.import_library(pid))
    m = _row(db, pid, "The Matrix")
    assert m["tmdb_id"] == "603"

    db.clear_tmdb_id("movie", m["id"])                            # reviewer says: that's wrong
    asyncio.run(library_importer.import_library(pid))             # rescan
    assert _row(db, pid, "The Matrix")["tmdb_id"] is None         # stays cleared
    assert db.get_library_match(pid, "The.Matrix.1999.mkv", "movie")["source"] == "manual"


def test_set_tmdb_id_by_hand_is_remembered(db, tmp_path, monkeypatch):
    _stub_two_dracula(monkeypatch)
    pid, root = _mk(db, tmp_path)
    _touch(root, "Movies/Dracula.mkv")
    asyncio.run(library_importer.import_library(pid))
    db.set_tmdb_id("movie", _row(db, pid, "Dracula")["id"], 138)
    asyncio.run(library_importer.import_library(pid))
    assert _row(db, pid, "Dracula")["tmdb_id"] == "138"


def test_manual_decision_on_non_library_item_is_a_no_op(db):
    pid = db.upsert_provider("xc1", "http://x", "u", "p")
    mid = db.upsert_movie("Some Movie", 2000)
    db.add_movie_source(mid, pid, "s1", "mp4")
    assert db.record_manual_library_match("movie", mid, tmdb_id="5") == 0


def test_series_manual_pick_uses_show_key(db, tmp_path, monkeypatch):
    async def fake_search(query, content_type, year):
        return [lm.Candidate("1", "The Office", 2005), lm.Candidate("2", "The Office", 2001)]
    monkeypatch.setattr(lm, "_search", fake_search)
    pid, root = _mk(db, tmp_path)
    _touch(root, "TV/The Office/Season 1/The Office S01E01.mkv")
    asyncio.run(library_importer.import_library(pid))
    conn = db._connect()
    try:
        sid = conn.execute("SELECT id FROM series").fetchone()["id"]
    finally:
        conn.close()
    db.set_tmdb_id("series", sid, 2316)
    d = db.get_library_match(pid, "TV/The Office", "series")
    assert (d["source"], d["tmdb_id"]) == ("manual", "2316")


# --- hung network mounts (NFS "hard") must fail fast, not hang ---------------

def test_hung_share_fails_with_clear_error_instead_of_hanging(db, tmp_path, monkeypatch):
    import threading
    release = threading.Event()

    def hung_probe(root):          # what a dead hard-mounted NFS export does
        release.wait(30)
        return True
    monkeypatch.setattr(library_importer, "_probe_root", hung_probe)
    monkeypatch.setattr(library_importer, "_PROBE_TIMEOUT_SECONDS", 0.3)
    pid, root = _mk(db, tmp_path)
    try:
        asyncio.run(library_importer.import_library(pid))
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "not responding" in str(exc)
    finally:
        release.set()


def test_playback_fs_check_is_bounded_and_does_not_block_the_loop(monkeypatch):
    import threading
    import time
    import xc_server
    release = threading.Event()
    monkeypatch.setattr(xc_server, "_check_local_file", lambda p, prov: release.wait(30) or p)
    monkeypatch.setattr(xc_server, "_LOCAL_FS_TIMEOUT_SECONDS", 0.3)

    async def scenario():
        ticks = []

        async def heartbeat():      # proves the event loop kept running during the hang
            for _ in range(6):
                ticks.append(time.monotonic())
                await asyncio.sleep(0.1)
        hb = asyncio.create_task(heartbeat())
        result = await xc_server._reachable_local_file("/mnt/nfs/x.mkv", {"provider_type": "library", "base_url": "/mnt/nfs"})
        await hb
        return result, len(ticks)
    try:
        result, ticks = asyncio.run(scenario())
    finally:
        release.set()
    assert result is None and ticks == 6


# --- audit fixes -------------------------------------------------------------

def test_unreadable_folder_does_not_purge_its_contents(db, tmp_path, monkeypatch):
    """os.walk silently skips a directory it can't list; that must NOT read as
    'the files were deleted' and wipe the show from the catalog."""
    _stub_tmdb(monkeypatch)
    pid, root = _mk(db, tmp_path)
    _touch(root, "TV/Breaking Bad (2008)/Season 1/Breaking Bad S01E01.mkv")
    _touch(root, "TV/Breaking Bad (2008)/Season 1/Breaking Bad S01E02.mkv")
    _touch(root, "Movies/The.Matrix.1999.mkv")
    asyncio.run(library_importer.import_library(pid))

    real_walk = os.walk

    def flaky_walk(top, followlinks=False, onerror=None):
        for dirpath, dirnames, filenames in real_walk(top, followlinks=followlinks):
            if os.path.basename(dirpath) == "Season 1":       # transient read failure
                if onerror:
                    onerror(OSError(13, "Permission denied", dirpath))
                continue
            yield dirpath, dirnames, filenames
    monkeypatch.setattr(library_importer.os, "walk", flaky_walk)

    r = asyncio.run(library_importer.import_library(pid))
    assert r["scan_errors"] == 1 and "could not be read" in r["removal_cleanup_skipped"]
    conn = db._connect()
    try:
        n = conn.execute("SELECT COUNT(*) c FROM episode_sources WHERE provider_id=?", (pid,)).fetchone()["c"]
    finally:
        conn.close()
    assert n == 2                                             # episodes survived


def test_concurrent_imports_of_one_library_are_serialised(db, tmp_path, monkeypatch):
    import threading
    import time
    _stub_tmdb(monkeypatch)
    pid, root = _mk(db, tmp_path)
    _touch(root, "The.Matrix.1999.mkv")
    state = {"now": 0, "max": 0}
    guard = threading.Lock()
    real_scan = library_importer._scan_root

    def slow_scan(r):
        with guard:
            state["now"] += 1
            state["max"] = max(state["max"], state["now"])
        time.sleep(0.3)
        try:
            return real_scan(r)
        finally:
            with guard:
                state["now"] -= 1
    monkeypatch.setattr(library_importer, "_scan_root", slow_scan)

    async def both():
        await asyncio.gather(library_importer.import_library(pid), library_importer.import_library(pid))
    asyncio.run(both())
    assert state["max"] == 1                                  # never two scans of one library at once


def test_delete_guard_needs_no_filesystem_access(db, tmp_path, monkeypatch):
    """A realpath()/stat on a dead hard-mounted share blocks forever, so the
    guard must decide from the path strings alone."""
    pid, root = _mk(db, tmp_path)

    def boom(*a, **k):
        raise AssertionError("filesystem touched")
    for name in ("realpath", "islink", "exists", "isfile", "isdir"):   # the calls that block on a dead mount
        monkeypatch.setattr(os.path, name, boom)
    inside = os.path.join(root, "Movies", "a.mkv")
    assert db._is_under_library_root(inside) is True
    assert db._is_under_library_root(os.path.join(root, "..", "elsewhere", "b.mkv")) is False   # ".." can't escape
    assert db._is_under_library_root(str(tmp_path / "dvr" / "c.mkv")) is False
    assert db._is_under_library_root(root + "-sibling/d.mkv") is False                        # prefix, not path, match


# --- rclone-mediated remote backends -----------------------------------------

def _mk_remote(db, backend="smb", remote_config=None):
    pid = db.upsert_provider("Remote NAS", "", "u", "p", provider_type="library",
                              library_backend=backend, library_remote_config=remote_config or {"host": "nas", "share": "media"})
    return pid


def test_remote_import_uses_rclone_listing_not_local_scan(db, monkeypatch):
    _stub_tmdb(monkeypatch)
    pid = _mk_remote(db)

    async def fake_probe(provider, remote_config):
        assert provider["id"] == pid
        return True

    async def fake_list(provider, remote_config):
        return [("Movies/The.Matrix.1999.mkv", 123), ("TV/Breaking Bad (2008)/Season 1/Breaking Bad S01E01.mkv", 456)]
    monkeypatch.setattr(library_importer.rclone_client, "probe", fake_probe)
    monkeypatch.setattr(library_importer.rclone_client, "list_remote", fake_list)

    r = asyncio.run(library_importer.import_library(pid))
    assert r["files_found"] == 2 and r["movies_created"] == 1 and r["episodes_imported"] == 1

    movies = _movie(db, pid)
    assert movies[0]["name"] == "The Matrix"
    assert movies[0]["local_file_path"] is None                 # no real local file -- resolved via rclone at play time
    assert movies[0]["provider_stream_id"] == "Movies/The.Matrix.1999.mkv"


def test_remote_probe_failure_is_a_clean_error_not_a_hang(db, monkeypatch):
    pid = _mk_remote(db)

    async def fail_probe(provider, remote_config):
        raise library_importer.rclone_client.RcloneError("connection refused")
    monkeypatch.setattr(library_importer.rclone_client, "probe", fail_probe)
    try:
        asyncio.run(library_importer.import_library(pid))
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "connection refused" in str(exc)


def test_remote_listing_failure_is_a_clean_error(db, monkeypatch):
    pid = _mk_remote(db)

    async def ok_probe(provider, remote_config):
        return True

    async def fail_list(provider, remote_config):
        raise library_importer.rclone_client.RcloneError("timed out")
    monkeypatch.setattr(library_importer.rclone_client, "probe", ok_probe)
    monkeypatch.setattr(library_importer.rclone_client, "list_remote", fail_list)
    try:
        asyncio.run(library_importer.import_library(pid))
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "timed out" in str(exc)


def test_remote_rescan_preserves_detail_same_as_local(db, monkeypatch):
    _stub_tmdb(monkeypatch)
    pid = _mk_remote(db)

    async def fake_probe(provider, remote_config):
        return True

    async def fake_list(provider, remote_config):
        return [("The.Matrix.1999.mkv", 100)]
    monkeypatch.setattr(library_importer.rclone_client, "probe", fake_probe)
    monkeypatch.setattr(library_importer.rclone_client, "list_remote", fake_list)

    asyncio.run(library_importer.import_library(pid))
    movie_id = _movie(db, pid)[0]["id"]
    db.set_movie_enrichment(movie_id, description="From TMDB", genre="Sci-Fi")

    asyncio.run(library_importer.import_library(pid))
    row = _movie(db, pid)[0]
    assert row["description"] == "From TMDB"


def test_remote_config_round_trips_through_upsert_and_is_redacted(db):
    pid = db.upsert_provider(
        "S3 lib", "", "AKIAEXAMPLE", "supersecretkey", provider_type="library",
        library_backend="s3", library_remote_config={"bucket": "movies", "region": "us-east-1"},
    )
    provider = db.get_provider(pid)
    assert provider["library_backend"] == "s3"
    assert provider["library_remote_config"] == {"bucket": "movies", "region": "us-east-1"}
    assert provider["password"] == "supersecretkey"          # full decrypt for internal/importer use

    conn = db._connect()
    try:
        raw = conn.execute("SELECT library_remote_config FROM providers WHERE id=?", (pid,)).fetchone()[0]
    finally:
        conn.close()
    assert "movies" not in raw                                # encrypted at rest, not plaintext JSON
