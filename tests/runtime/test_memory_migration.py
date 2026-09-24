import sqlite3

import pytest

from server.db import memory_migration as migration


@pytest.fixture
def legacy(tmp_path, monkeypatch):
    conn = sqlite3.connect(tmp_path / "old.sqlite")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE schema_migrations (migration_id TEXT PRIMARY KEY,checksum TEXT,applied_at REAL);
        CREATE TABLE assistant_memories (memory_id TEXT PRIMARY KEY,template_id TEXT,body TEXT,
            source_groups TEXT DEFAULT '[]',source TEXT DEFAULT 'manual',ts REAL);
        CREATE TABLE runtime_runs (run_id TEXT PRIMARY KEY,session_id TEXT);
        INSERT INTO assistant_memories VALUES ('m','a','original memory','[]','manual',1);
        INSERT INTO runtime_runs VALUES ('legacy_run','session');
    """)
    events = []
    monkeypatch.setattr(migration, "append_journal_event", lambda step, stage, **kw: events.append(stage))
    yield conn, tmp_path, events
    conn.close()


def test_migration_backup_idempotence_and_legacy_run_binding(legacy):
    conn, path, events = legacy
    migration.migrate(conn)
    backup_path = path / "backups" / (migration.MIGRATION_ID + ".sqlite3")
    with sqlite3.connect(backup_path) as backup:
        assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert backup.execute("SELECT body FROM assistant_memories").fetchone()[0] == "original memory"
        assert "version" not in {r[1] for r in backup.execute("PRAGMA table_info(assistant_memories)")}
    assert backup_path.stat().st_mode & 0o777 == 0o600
    assert conn.execute("SELECT epoch FROM session_run_fences WHERE run_id='legacy_run'").fetchone()[0] == 0
    before = backup_path.read_bytes()
    migration.migrate(conn)
    assert backup_path.read_bytes() == before
    assert events == ["prepared", "committed"]


def test_migration_failure_rolls_back_all_stages_and_can_retry(legacy, monkeypatch):
    conn, path, events = legacy
    original = migration.migrate_jobs
    def fail(connection):
        connection.execute("CREATE TABLE halfway(x)")
        raise RuntimeError("injected failure")
    monkeypatch.setattr(migration, "migrate_jobs", fail)
    with pytest.raises(RuntimeError, match="injected"):
        migration.migrate(conn)
    assert conn.execute("SELECT body FROM assistant_memories").fetchone()[0] == "original memory"
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert not ({"halfway", "session_run_fences", "session_fences", "memory_revisions"} & tables)
    assert "version" not in {r[1] for r in conn.execute("PRAGMA table_info(assistant_memories)")}
    assert events == ["prepared", "rolled_back"]
    monkeypatch.setattr(migration, "migrate_jobs", original)
    migration.migrate(conn)
    assert conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 1


def test_migration_checksum_mismatch_does_not_change_data(legacy):
    conn, _, _ = legacy
    migration.migrate(conn)
    conn.execute("UPDATE schema_migrations SET checksum='wrong'")
    conn.commit()
    with pytest.raises(RuntimeError, match="checksum"):
        migration.migrate(conn)
    assert conn.execute("SELECT body FROM assistant_memories").fetchone()[0] == "original memory"


@pytest.mark.parametrize("corrupt", [True, False])
def test_existing_invalid_backup_blocks_migration(legacy, corrupt):
    conn, path, events = legacy
    directory = path / "backups"
    directory.mkdir()
    backup_path = directory / (migration.MIGRATION_ID + ".sqlite3")
    if corrupt:
        backup_path.write_bytes(b"incomplete backup")
    else:
        with sqlite3.connect(backup_path) as empty:
            empty.execute("CREATE TABLE unrelated(x)")
    with pytest.raises((sqlite3.DatabaseError, RuntimeError)):
        migration.migrate(conn)
    assert "version" not in {r[1] for r in conn.execute("PRAGMA table_info(assistant_memories)")}
    assert events == []
