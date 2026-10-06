"""vod_manager-p7h: smart-category evaluation reads its matches outside the
write lock, so a match can be merged away before bulk placement runs. The
resulting FOREIGN KEY failure used to leave the placement connection's write
transaction open, holding SQLite's write lock for minutes: every other writer
timed out on 'database is locked' and the whole server froze. Placement must
skip vanished ids and never leave a transaction open, whatever happens."""

import sqlite3

import pytest

import vod_db


def _another_connection_can_write(db):
    """True when a second connection can take SQLite's write lock at once."""
    conn = sqlite3.connect(str(vod_db.DB_PATH), timeout=0.2)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.rollback()
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        conn.close()


def _movie(db, provider_id, name, stream_id):
    db.bulk_import_movies(provider_id, [{
        "name": name, "year": 2001, "provider_stream_id": stream_id,
        "container_extension": "mp4", "raw_name": name, "_has_detail": True,
    }])
    return db.get_movie_by_name_year(name, 2001)["id"]


def _series(db, provider_id, name, series_id):
    db.bulk_import_series(provider_id, [{
        "name": name, "year": 2001, "provider_series_id": series_id,
        "raw_name": name, "_has_detail": True,
    }])
    return next(r["id"] for r in db.list_series(limit=50) if r["name"] == name)


def test_bulk_place_movies_skips_vanished_id_and_leaves_no_open_transaction(db):
    provider_id = db.upsert_provider("prov1", "http://a.invalid", "u", "p")
    real = _movie(db, provider_id, "Real Movie", "m1")
    category_id = db.upsert_category("Smart", "movie")

    placed = db.bulk_place_movies_in_category([real, 987654], category_id)

    assert placed == 1
    assert _another_connection_can_write(db)


def test_bulk_place_series_skips_vanished_id_and_leaves_no_open_transaction(db):
    provider_id = db.upsert_provider("prov1", "http://a.invalid", "u", "p")
    real = _series(db, provider_id, "Real Show", "s1")
    category_id = db.upsert_category("SmartS", "series")

    placed = db.bulk_place_series_in_category([real, 987654], category_id)

    assert placed == 1
    assert _another_connection_can_write(db)


def test_bulk_place_releases_write_lock_when_the_insert_itself_fails(db, monkeypatch):
    provider_id = db.upsert_provider("prov1", "http://a.invalid", "u", "p")
    real = _movie(db, provider_id, "Real Movie", "m1")
    category_id = db.upsert_category("Smart", "movie")

    real_connect = vod_db._connect

    class FailingInsert:
        """Proxy connection whose executemany fails after a write began."""
        def __init__(self, conn):
            self._conn = conn

        def executemany(self, sql, rows):
            self._conn.execute("INSERT INTO movie_category_placements "
                               "(movie_id, category_id, export_stream_id, name_suffix) VALUES (?,?,?,?)",
                               (real, category_id, 1, ""))
            raise sqlite3.IntegrityError("boom")

        def __getattr__(self, name):
            return getattr(self._conn, name)

    monkeypatch.setattr(vod_db, "_connect", lambda: FailingInsert(real_connect()))

    with pytest.raises(sqlite3.IntegrityError):
        db.bulk_place_movies_in_category([real], category_id)

    monkeypatch.undo()
    assert _another_connection_can_write(db)
