"""'Save to this device': a finished download can be fetched through a short-lived, single-file link."""

import asyncio
import time

import pytest
from fastapi import HTTPException

import vod_db
import vod_downloader
import vod_routes

from test_downloads import FILE_BYTES, _movie, _run, dl, server  # noqa: F401


def _finished(db, server):
    mid, _ = _movie(db, server)
    vod_downloader.enqueue(movie_ids=[mid])
    assert _run(1)["status"] == "done"


def test_link_serves_exactly_the_finished_file(db, server, dl):
    _finished(db, server)
    link = asyncio.run(vod_routes.download_file_link(1))["url"]
    token = link.rstrip("/").rsplit("/", 1)[-1]
    resp = asyncio.run(vod_routes.download_file(token))
    with open(resp.path, "rb") as fh:
        assert fh.read() == FILE_BYTES
    assert str(resp.filename).endswith("Some Movie (2001).mp4")


def test_no_link_for_an_unfinished_job(db, server, dl):
    mid, _ = _movie(db, server)
    vod_downloader.enqueue(movie_ids=[mid])
    with pytest.raises(HTTPException) as e:
        asyncio.run(vod_routes.download_file_link(1))
    assert e.value.status_code == 404


def test_expired_and_unknown_tokens_are_refused(db, server, dl):
    _finished(db, server)
    token = vod_downloader.issue_file_link(1)
    vod_downloader._file_tokens[token] = (1, time.time() - 1)
    for bad in (token, "not-a-real-token"):
        with pytest.raises(HTTPException) as e:
            asyncio.run(vod_routes.download_file(bad))
        assert e.value.status_code == 404


def test_a_path_outside_the_download_folder_is_never_served(db, server, dl, tmp_path):
    _finished(db, server)
    secret = tmp_path / "secret.txt"
    secret.write_text("nope")
    vod_db.update_download_job(1, final_path="../secret.txt")
    assert vod_downloader.file_for_job(1) is None
    assert vod_downloader.issue_file_link(1) is None
