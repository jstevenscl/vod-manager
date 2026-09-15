import os
import sys
import tempfile
from pathlib import Path

_tmp_data_dir = tempfile.mkdtemp(prefix="vod_manager_test_")
os.environ["DATA_DIR"] = _tmp_data_dir

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import config
import vod_db


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """Fresh, empty sqlite DB per test -- points vod_db.DB_PATH at a unique
    file under pytest's own tmp_path so tests never share or leak state.
    Also points config.CONFIG_FILE at a fresh file -- config.json otherwise
    lives at the shared DATA_DIR set once at module import time above, so
    without this a setting saved in one test (e.g. save_enabled_languages)
    leaks into every later test in the same pytest run."""
    db_path = tmp_path / "vod_db.sqlite"
    monkeypatch.setattr(vod_db, "DB_PATH", db_path)
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "config.json")
    vod_db.init_db()
    return vod_db
