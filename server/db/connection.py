"""SQLite connection helpers."""

from __future__ import annotations

import sqlite3

from avent_paths import data_root, ensure_data_dirs


def _db_path():
    ensure_data_dirs()
    return data_root() / "data" / "avent.db"


def get_connection() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn
