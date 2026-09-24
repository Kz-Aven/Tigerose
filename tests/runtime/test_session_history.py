import json
import sqlite3
import threading

import pytest

from server.runtime import session_history as history
from session_store import SessionManager, SessionVersionConflict


@pytest.fixture
def environment(tmp_path, monkeypatch):
    db = tmp_path / "history.sqlite"
    def connect():
        conn = sqlite3.connect(db, timeout=2)
        conn.row_factory = sqlite3.Row
        return conn
    monkeypatch.setattr(history, "get_connection", connect)
    with connect() as conn:
        history.migrate(conn)
        conn.executescript("""
        CREATE TABLE assistant_messages(message_id TEXT PRIMARY KEY,template_id TEXT,session_id TEXT,
            role TEXT,content TEXT,meta TEXT,ts REAL);
        CREATE TABLE pending_questions(question_id TEXT PRIMARY KEY,session_id TEXT,status TEXT,payload TEXT,
            answer TEXT,run_id TEXT,instance_id TEXT,answered_at REAL);
        CREATE TABLE feed_events(event_id TEXT PRIMARY KEY,content TEXT,speaker_type TEXT,meta TEXT,
            group_id TEXT,speaker_id TEXT);
        CREATE TABLE memory_jobs(job_id TEXT,session_id TEXT,status TEXT);
        CREATE TABLE memory_candidates(candidate_id TEXT,job_id TEXT,status TEXT);
        """)
    sm = SessionManager(tmp_path, {"session": {"persist_dir": ".sessions"}})
    return sm, connect


def add_message(connect, sid, mid, content, *, role="user", meta=None, ts=1, record=True):
    with connect() as conn:
        conn.execute("INSERT INTO assistant_messages VALUES (?,?,?,?,?,?,?)", (mid, "a", sid, role, content, json.dumps(meta or {}), ts))
        if record:
            history.record(conn, sid, "message", mid, run_id=(meta or {}).get("run_id", ""))


def test_legacy_sources_order_dedup_and_compact_cursor(environment):
    sm, connect = environment
    state = sm.create_session("assistant_dm", "user:local:assistant:a")
    sid = state.meta.session_id
    state.messages = [{"role": "user", "content": "previous API", "source_kind": "message", "source_id": "old"}]
    add_message(connect, sid, "old", "previous API", record=False, ts=1)
    add_message(connect, sid, "new", "next request", ts=3)
    with connect() as conn:
        conn.execute("INSERT INTO pending_questions VALUES (?,?,?,?,?,?,?,?)", (
            "q", sid, "answered", json.dumps({"questions": [{"id": "api", "header": "接口"}]}),
            json.dumps({"answers": [{"question_id": "api", "other_text": "https://example.com/api"}]}), "r", "", 2))
    history.project(state)
    assert len(state.messages) == 3
    assert [m["source_id"] for m in state.messages] == ["old", "q", "new"]
    assert "https://example.com/api" in state.messages[1]["content"]
    history.project(state)
    assert len(state.messages) == 3
    cursor = state.context["history_cursor"]
    state.messages = [{"role": "assistant", "content": "compacted summary"}]
    history.project(state)
    assert len(state.messages) == 1 and state.context["history_cursor"] == cursor


def test_assistant_draft_and_final_both_retained_once(environment):
    sm, connect = environment
    state = sm.create_session("assistant_dm", "user:local:assistant:a")
    state.messages = [{"role": "assistant", "content": "final", "run_id": "r"}]
    add_message(connect, state.meta.session_id, "draft", "draft", role="assistant", meta={"run_id": "r", "intermediate": True})
    add_message(connect, state.meta.session_id, "final", "final", role="assistant", meta={"run_id": "r"}, ts=2)
    history.project(state)
    history.project(state)
    assert len(state.messages) == 2
    assert [m["content"] for m in state.messages] == ["draft", "final"]


def test_group_only_addressed_agent_and_attachments(environment):
    sm, connect = environment
    state = sm.create_session("group_agent", "group:g:agent:i")
    with connect() as conn:
        conn.execute("INSERT INTO feed_events VALUES (?,?,?,?,?,?)", (
            "f", "read attachment", "user", json.dumps({"attachments": [{"name": "doc", "path": "/tmp/doc.md"}]}), "g", "user"))
        history.record(conn, state.meta.session_id, "feed", "f", run_id="r", instance_id="i")
    history.project(state)
    assert state.messages[0]["role"] == "user" and "/tmp/doc.md" in state.messages[0]["content"]
    other = sm.create_session("group_agent", "group:g:agent:other")
    with connect() as conn:
        history.record(conn, other.meta.session_id, "feed", "f", instance_id="i")
    with pytest.raises(RuntimeError, match="another agent"):
        history.project(other)


@pytest.mark.parametrize("action", ["set", "resume"])
def test_group_goal_commands_keep_user_role(environment, action):
    sm, connect = environment
    state = sm.create_session("group_agent", "group:g:agent:i")
    with connect() as conn:
        conn.execute("INSERT INTO feed_events VALUES (?,?,?,?,?,?)", (
            "goal", "user goal request", "system", json.dumps({"goal_command": action}), "g", "user"))
        history.record(conn, state.meta.session_id, "feed", "goal", run_id="r", instance_id="i")
    history.project(state)
    assert state.messages[0]["role"] == "user"


def test_identical_text_with_distinct_source_ids_is_not_deduplicated(environment):
    sm, connect = environment
    state = sm.create_session("assistant_dm", "user:local:assistant:a")
    add_message(connect, state.meta.session_id, "m1", "same text", ts=1)
    add_message(connect, state.meta.session_id, "m2", "same text", ts=2)
    history.project(state)
    assert len(state.messages) == 2


def test_epoch_clear_blocks_old_writers_and_cancels_candidates(environment):
    sm, connect = environment
    state = sm.create_session("assistant_dm", "user:local:assistant:a")
    sid = state.meta.session_id
    add_message(connect, sid, "before", "before")
    history.project(state)
    sm.save(state)
    stale = sm.load(sid)
    with connect() as conn:
        conn.execute("INSERT INTO memory_jobs VALUES ('j',?,'running')", (sid,))
        conn.execute("INSERT INTO memory_candidates VALUES ('c','j','pending')")
        conn.execute("INSERT INTO pending_questions VALUES ('q',?,'pending','{}',NULL,'','',0)", (sid,))
    def clear(conn, epoch):
        conn.execute("DELETE FROM assistant_messages WHERE session_id=?", (sid,))
        sm.clear_session_contents(sid, session_epoch=epoch, _fence_connection=conn)
    with sm._lock:
        history.invalidate(sid, on_invalidate=clear)
    with pytest.raises(RuntimeError, match="generation changed"):
        sm.save(stale, retries=2)
    with connect() as conn:
        assert conn.execute("SELECT status FROM memory_candidates").fetchone()[0] == "cancelled"
        assert conn.execute("SELECT status FROM pending_questions").fetchone()[0] == "cancelled"
        with pytest.raises(RuntimeError, match="generation changed"):
            history.record(conn, sid, "message", "late", expected_epoch=0)
    add_message(connect, sid, "after", "new epoch")
    current = sm.load(sid)
    history.project(current)
    assert [m["content"] for m in current.messages] == ["new epoch"]
    assert current.context["session_epoch"] == 1


def test_new_message_waits_until_clear_commit(environment):
    sm, connect = environment
    state = sm.create_session("assistant_dm", "user:local:assistant:a")
    sid = state.meta.session_id
    entered = threading.Event()
    finished = threading.Event()
    errors = []
    def receive():
        entered.set()
        try:
            add_message(connect, sid, "after", "survives clear")
        except Exception as exc:
            errors.append(exc)
        finally:
            finished.set()
    def clear(conn, epoch):
        worker.start()
        assert entered.wait(1)
        assert not finished.wait(.05)
        conn.execute("DELETE FROM assistant_messages WHERE session_id=?", (sid,))
        sm.clear_session_contents(sid, session_epoch=epoch, _fence_connection=conn)
    worker = threading.Thread(target=receive)
    with sm._lock:
        history.invalidate(sid, on_invalidate=clear)
    worker.join(3)
    assert finished.is_set() and not errors
    current = sm.load(sid)
    history.project(current)
    assert [m["content"] for m in current.messages] == ["survives clear"]


def test_same_length_history_cas_and_metadata_retry(environment):
    sm, _ = environment
    state = sm.create_session("cli", "cli:default")
    state.messages = [{"role": "user", "content": "old"}]
    sm.save(state)
    stale, winner = sm.load(state.meta.session_id), sm.load(state.meta.session_id)
    winner.messages[0]["content"] = "new"
    sm.save(winner)
    stale.messages[0]["content"] = "bad"
    with pytest.raises(SessionVersionConflict):
        sm.save(stale, retries=2)
    writer = sm.load(state.meta.session_id)
    sm.patch_meta(state.meta.session_id, title="renamed")
    writer.messages.append({"role": "assistant", "content": "reply"})
    sm.save(writer, retries=2)
    result = sm.load(state.meta.session_id)
    assert result.meta.title == "renamed" and len(result.messages) == 2


def test_purged_legacy_writer_cannot_recreate_session(environment):
    sm, _ = environment
    state = sm.create_session("cli", "cli:default")
    sm.purge_session(state.meta.session_id)
    with pytest.raises(SessionVersionConflict, match="removed"):
        sm.save(state)
    assert sm.load(state.meta.session_id) is None
