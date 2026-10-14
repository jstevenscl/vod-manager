"""vod_manager-03r: a vod_db function that raises mid-write must not leave its
connection (and SQLite's write lock) open until garbage collection."""

import ast
import gc
import sqlite3
from pathlib import Path

import pytest

import vod_db


def _is_connect(node):
    return (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
            and getattr(node.value.func, "id", "") == "_connect")


def test_every_connection_in_vod_db_is_closed_in_a_finally():
    tree = ast.parse(Path(vod_db.__file__).read_text(encoding="utf-8"))
    unguarded = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(fn):
            for field in ("body", "orelse", "finalbody"):
                stmts = getattr(node, field, None)
                if not isinstance(stmts, list):
                    continue
                for i, st in enumerate(stmts):
                    if _is_connect(st) and i + 1 < len(stmts):
                        nxt = stmts[i + 1]
                        if not (isinstance(nxt, ast.Try) and nxt.finalbody):
                            unguarded.append(f"{fn.name}:{st.lineno}")
    assert not unguarded, f"connections without try/finally: {sorted(set(unguarded))}"


class _FailsAfterFirstWrite:
    """Connection proxy: the first write statement goes through, the next
    statement raises -- the shape of a mid-function failure (e.g. an FK error)."""

    def __init__(self, conn):
        self._conn = conn
        self._wrote = False

    def execute(self, sql, *args):
        if self._wrote:
            raise sqlite3.OperationalError("boom after write")
        result = self._conn.execute(sql, *args)
        if sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")):
            self._wrote = True
        return result

    def __getattr__(self, name):
        return getattr(self._conn, name)


def test_a_failure_after_a_write_releases_the_write_lock_immediately(db, monkeypatch):
    real_connect = vod_db._connect
    monkeypatch.setattr(vod_db, "_connect", lambda: _FailsAfterFirstWrite(real_connect()))
    gc.disable()  # prove the function itself releases the lock, not the garbage collector
    try:
        with pytest.raises(sqlite3.OperationalError) as held:  # keeps the traceback (and its frames) alive
            vod_db.record_source_success("movie", 1)
        monkeypatch.setattr(vod_db, "_connect", real_connect)
        other = sqlite3.connect(str(db.DB_PATH), timeout=0.2)
        try:
            other.execute("BEGIN IMMEDIATE")  # raises "database is locked" if a write txn leaked
            other.rollback()
        finally:
            other.close()
    finally:
        gc.enable()
    assert held
