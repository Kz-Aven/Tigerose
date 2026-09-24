"""Versioned local cache for connector CLI schema responses."""

from __future__ import annotations

import sqlite3
import time

from server.connectors.store import connector_root

_TTL_SECONDS = 24 * 60 * 60


def _connect(connector_id: str) -> sqlite3.Connection:
    path = connector_root(connector_id) / "schema_cache.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_cache (
            cli_version TEXT NOT NULL,
            command_path TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at REAL NOT NULL,
            PRIMARY KEY (cli_version, command_path)
        )
        """
    )
    return conn


def get(connector_id: str, cli_version: str, command_path: str) -> str | None:
    conn = _connect(connector_id)
    try:
        row = conn.execute(
            "SELECT content, created_at FROM schema_cache WHERE cli_version = ? AND command_path = ?",
            (cli_version, command_path),
        ).fetchone()
    finally:
        conn.close()
    if row is None or time.time() - float(row[1]) > _TTL_SECONDS:
        return None
    return str(row[0])


def put(connector_id: str, cli_version: str, command_path: str, content: str) -> None:
    conn = _connect(connector_id)
    try:
        conn.execute(
            """
            INSERT INTO schema_cache (cli_version, command_path, content, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(cli_version, command_path) DO UPDATE SET
                content = excluded.content,
                created_at = excluded.created_at
            """,
            (cli_version, command_path, content, time.time()),
        )
        conn.commit()
    finally:
        conn.close()


def clear(connector_id: str) -> None:
    conn = _connect(connector_id)
    try:
        conn.execute("DELETE FROM schema_cache")
        conn.commit()
    finally:
        conn.close()
