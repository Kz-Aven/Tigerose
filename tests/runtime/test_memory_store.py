import json
import sqlite3

import pytest

from server.db import memory_store as store


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "memory.sqlite"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE assistant_memories (memory_id TEXT PRIMARY KEY,template_id TEXT,body TEXT,
          source_groups TEXT DEFAULT '[]',source TEXT DEFAULT 'manual',ts REAL);
        CREATE TABLE project_groups (group_id TEXT PRIMARY KEY,status TEXT);
        CREATE TABLE group_memberships (instance_id TEXT,group_id TEXT,template_id TEXT);
        CREATE TABLE assistant_messages (message_id TEXT PRIMARY KEY,template_id TEXT,role TEXT,content TEXT,meta TEXT);
        CREATE TABLE feed_events (event_id TEXT PRIMARY KEY,group_id TEXT,speaker_id TEXT,meta TEXT);
        INSERT INTO project_groups VALUES ('g1','active'),('g2','active');
        INSERT INTO group_memberships VALUES ('i1','g1','a'),('i2','g2','a');
        INSERT INTO assistant_memories VALUES ('old','a','旧接口 https://example.com/api','["g1"]','manual',1);
    """)
    store.migrate(conn)
    conn.commit()
    yield conn, path
    conn.close()


def test_migration_preserves_legacy_and_repeats(database):
    conn, _ = database
    store.migrate(conn)
    old = store.get(conn, "a", "old")
    assert old["body"] == "旧接口 https://example.com/api"
    assert (old["scope_kind"], old["scope_id"], old["type"], old["updated_at"]) == ("assistant", "a", "unknown", 1)
    assert old["source_refs"] == []
    assert len(store.revisions(conn, "a", "old")) == 1


def test_migration_checksum_failure_preserves_data(database):
    conn, _ = database
    conn.execute("UPDATE memory_migrations SET checksum='wrong'")
    with pytest.raises(RuntimeError, match="checksum"):
        store.migrate(conn)
    assert store.get(conn, "a", "old")["version"] == 1


def test_scope_owner_membership_filter(database):
    conn, _ = database
    g1 = store.create(conn, "a", "群一事实", scope_kind="group", scope_id="g1")
    store.create(conn, "a", "群二事实", scope_kind="group", scope_id="g2")
    store.create(conn, "b", "另一助理事实")
    assert {m["memory_id"] for m in store.authorized(conn, "a")} == {"old"}
    assert {m["memory_id"] for m in store.authorized(conn, "a", "g1")} == {"old", g1["memory_id"]}
    conn.execute("DELETE FROM group_memberships WHERE group_id='g1'")
    with pytest.raises(ValueError):
        store.authorized(conn, "a", "g1")
    with pytest.raises(ValueError):
        store.create(conn, "b", "越权", scope_kind="group", scope_id="g2")


def test_cas_softdelete_restore_and_revision(database):
    conn, _ = database
    new = store.update(conn, "a", "old", body="新接口", expected_version=1)
    with pytest.raises(store.MemoryConflict):
        store.update(conn, "a", "old", body="陈旧结果", expected_version=1)
    assert new["version"] == 2
    store.update(conn, "a", "old", status="deleted", expected_version=2)
    assert store.authorized(conn, "a") == []
    restored = store.restore(conn, "a", "old", 1, 3)
    assert restored["version"] == 4 and "https://" in restored["body"]
    assert len(store.revisions(conn, "a", "old")) == 4
    assert store.get(conn, "b", "old", include_deleted=True) is None


def test_background_needs_version_and_cannot_restore(database):
    conn, _ = database
    with pytest.raises(ValueError, match="expected_version"):
        store.update(conn, "a", "old", body="changed", actor="worker")
    store.update(conn, "a", "old", status="deleted", expected_version=1)
    with pytest.raises(store.MemoryConflict):
        store.update(conn, "a", "old", status="active", expected_version=2, actor="worker")


def test_confirm_source_failure_is_atomic(database):
    conn, _ = database
    with pytest.raises(LookupError), conn:
        store.confirm(conn, "a", "合法内容", message_id="missing")
    assert len(store.list_memories(conn, "a")) == 1


def test_confirm_idempotent_and_updates_source(database):
    conn, _ = database
    conn.execute("INSERT INTO assistant_messages VALUES ('m','a','assistant','text',?)", (json.dumps({"remember_proposal": {"body": "remember"}}),))
    with conn:
        first = store.confirm(conn, "a", "记住接口", message_id="m", source="confirm")
    with conn:
        retry = store.confirm(conn, "a", "记住接口", message_id="m", source="confirm")
    assert first["memory_id"] == retry["memory_id"]
    assert len(store.list_memories(conn, "a")) == 2
    assert json.loads(conn.execute("SELECT meta FROM assistant_messages").fetchone()[0])["remember_confirmed"]
    with pytest.raises(LookupError):
        store.confirm(conn, "b", "越权", message_id="m")


def test_group_proposal_owner_and_scope(database):
    conn, _ = database
    conn.execute("INSERT INTO feed_events VALUES ('e','g1','i1',?)", (json.dumps({"remember_proposal": {"body": "remember"}}),))
    result = store.confirm(conn, "a", "群知识", event_id="e", group_id="g1")
    assert result["scope_kind"] == "group" and result["scope_id"] == "g1"
    assert result["memory_id"] not in {m["memory_id"] for m in store.authorized(conn, "a")}
    with pytest.raises(ValueError):
        store.confirm(conn, "b", "越权", event_id="e", group_id="g1")


def test_secret_validation_in_body_and_summary(database):
    conn, _ = database
    for kwargs in ({"body": "api_key=abcdefghijklmnop"}, {"body": "ordinary", "summary": "password=abcdefgh"}):
        with pytest.raises(ValueError, match="credentials"):
            store.create(conn, "a", **kwargs)


def test_migration_rolls_back_added_columns_on_failure(tmp_path):
    conn = sqlite3.connect(tmp_path / "failure.sqlite")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE assistant_memories (memory_id TEXT PRIMARY KEY,template_id TEXT,body TEXT,
          source_groups TEXT DEFAULT '[]',source TEXT DEFAULT 'manual',ts REAL);
        INSERT INTO assistant_memories VALUES ('old','a','original','[]','manual',1);
        CREATE TRIGGER reject_migration BEFORE UPDATE ON assistant_memories
        BEGIN SELECT RAISE(ABORT,'test failure'); END;
    """)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            store.migrate(conn)
        assert "version" not in {r[1] for r in conn.execute("PRAGMA table_info(assistant_memories)")}
        assert conn.execute("SELECT body FROM assistant_memories").fetchone()[0] == "original"
        conn.execute("DROP TRIGGER reject_migration")
        store.migrate(conn)
        assert store.get(conn, "a", "old")["version"] == 1
    finally:
        conn.close()


def test_dismiss_prevents_confirmation(database):
    conn, _ = database
    conn.execute("INSERT INTO assistant_messages VALUES ('m','a','assistant','text',?)", (json.dumps({"remember_proposal": "remember"}),))
    store.dismiss(conn, "a", message_id="m")
    with pytest.raises(ValueError, match="pending"):
        store.confirm(conn, "a", "记住", message_id="m")


def test_existing_crud_and_api_conflict(database, monkeypatch):
    from fastapi import HTTPException
    from server.api import assistants
    from server.db import repos
    _, path = database
    def connect():
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        return conn
    monkeypatch.setattr(repos, "get_connection", connect)
    monkeypatch.setattr(repos, "get_template", lambda _: {"template_id": "a"})
    memory = assistants.confirm_memory("a", assistants.MemoryConfirm(body="手动保存"))
    memory = assistants.patch_memory("a", memory["memory_id"], assistants.MemoryUpdate(body="旧客户端仍可编辑"))
    assert memory["version"] == 2
    with pytest.raises(HTTPException) as err:
        assistants.patch_memory("a", memory["memory_id"], assistants.MemoryUpdate(body="旧结果", expected_version=1))
    assert err.value.status_code == 409
    assert repos.get_assistant_memory("a", memory["memory_id"])["body"] == "旧客户端仍可编辑"
    assert assistants.delete_memory("a", memory["memory_id"], expected_version=2) == {"ok": True}
    restored = assistants.restore_memory("a", memory["memory_id"], assistants.MemoryRestore(revision_version=1, expected_version=3))
    assert restored["body"] == "手动保存" and restored["version"] == 4
