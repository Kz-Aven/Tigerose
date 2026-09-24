"""One transactional migration with a verified pre-change SQLite backup."""

import hashlib
import sqlite3
from pathlib import Path

from server.db.memory_store import migrate as migrate_memories
from server.runtime.memory_jobs import migrate as migrate_jobs
from server.runtime.run_fence import migrate as migrate_runs
from server.runtime.session_history import migrate as migrate_history
from server.runtime.migration_journal import append_journal_event

MIGRATION_ID = "memory_reliability_v1"
CHECKSUM = hashlib.sha256(b"session_sources_v1;session_run_fences_v1;structured_memories_v1;memory_jobs_v1").hexdigest()


def migrate(conn):
    prior = conn.execute("SELECT checksum FROM schema_migrations WHERE migration_id=?", (MIGRATION_ID,)).fetchone()
    if prior:
        if prior[0] != CHECKSUM:
            raise RuntimeError("memory reliability migration checksum mismatch")
        return
    conn.commit()
    database = conn.execute("PRAGMA database_list").fetchone()[2]
    backup_path = ""
    if database:
        directory = Path(database).parent / "backups"
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        destination = directory / (MIGRATION_ID + ".sqlite3")
        if not destination.exists():
            import os
            import tempfile
            fd, temporary = tempfile.mkstemp(prefix=MIGRATION_ID + "-", suffix=".tmp", dir=directory)
            os.close(fd)
            try:
                with sqlite3.connect(temporary) as backup:
                    conn.backup(backup)
                    if backup.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise RuntimeError("memory migration backup integrity check failed")
                os.replace(temporary, destination)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        with sqlite3.connect(f"file:{destination}?mode=ro", uri=True) as backup:
            if backup.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("memory migration backup integrity check failed")
            tables = {r[0] for r in backup.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {"assistant_memories", "schema_migrations"}.issubset(tables):
                raise RuntimeError("memory migration backup is missing original tables")
        backup_path = str(destination)
    append_journal_event(MIGRATION_ID, "prepared", checksum=CHECKSUM, payload={"backup": backup_path})
    try:
        conn.execute("BEGIN IMMEDIATE")
        migrate_history(conn)
        migrate_runs(conn)
        migrate_memories(conn)
        migrate_jobs(conn)
        conn.execute("INSERT INTO schema_migrations VALUES (?, ?, strftime('%s','now'))", (MIGRATION_ID, CHECKSUM))
        conn.commit()
    except BaseException:
        conn.rollback()
        append_journal_event(MIGRATION_ID, "rolled_back", checksum=CHECKSUM)
        raise
    append_journal_event(MIGRATION_ID, "committed", checksum=CHECKSUM, payload={"backup": backup_path})
