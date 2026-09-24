import sqlite3

import pytest

from server.db import repos, schema
from server.runtime import run_fence, session_history
from server.runtime.run_coordinator import SessionRunCoordinator


@pytest.fixture
def runtime_db(tmp_path, monkeypatch):
    path = tmp_path / "runtime.sqlite"
    def connect():
        conn = sqlite3.connect(path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn
    monkeypatch.setattr(schema, "get_connection", connect)
    monkeypatch.setattr(repos, "get_connection", connect)
    monkeypatch.setattr(session_history, "get_connection", connect)
    monkeypatch.setattr("server.db.memory_migration.append_journal_event", lambda *a, **k: None)
    schema.init_db()
    return connect


def test_late_final_rejected_after_clear_but_new_run_can_write(runtime_db):
    repos.create_runtime_run("old", "session")
    session_history.invalidate("session")
    with pytest.raises(RuntimeError, match="generation"):
        repos.add_assistant_message("a", "assistant", "late final", session_id="session", meta={"run_id": "old"})
    repos.create_runtime_run("new", "session")
    repos.add_assistant_message("a", "assistant", "new final", session_id="session", meta={"run_id": "new"})
    conn = runtime_db()
    try:
        assert [r[0] for r in conn.execute("SELECT content FROM assistant_messages")] == ["new final"]
        assert [r[0] for r in conn.execute("SELECT session_epoch FROM session_history_events")] == [1]
    finally:
        conn.close()


def test_late_question_answer_rolls_back_answer_and_pointer(runtime_db):
    repos.create_runtime_run("old", "session")
    pending = repos.create_pending_question(tool_call_id="call", goal_id="", run_id="old", session_id="session",
        channel="assistant:a", surface="assistant_dm", template_id="a", group_id="", instance_id="",
        payload={"questions": []})
    session_history.invalidate("session")
    assert repos.answer_pending_question(pending["question_id"], [{"question_id": "q", "other_text": "late address"}]) is None
    assert repos.get_pending_question(pending["question_id"])["status"] == "cancelled"
    assert repos.get_pending_question(pending["question_id"])["answer"] is None
    conn = runtime_db()
    try:
        assert conn.execute("SELECT COUNT(*) FROM session_history_events").fetchone()[0] == 0
        with pytest.raises(RuntimeError, match="generation"):
            run_fence.validate(conn, "unbound_old_run", "session")
    finally:
        conn.close()


def test_enqueue_database_error_releases_busy_and_allows_retry(monkeypatch):
    coordinator = SessionRunCoordinator()
    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("disk full")
    monkeypatch.setattr(repos, "create_runtime_run", fail)
    with pytest.raises(sqlite3.OperationalError):
        coordinator.enqueue("session")
    assert not coordinator.is_busy("session")
    monkeypatch.setattr(repos, "create_runtime_run", lambda *a, **k: {})
    assert coordinator.enqueue("session").startswith("run_")


def test_begin_database_error_releases_lock_and_token(monkeypatch):
    coordinator = SessionRunCoordinator()
    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("locked")
    monkeypatch.setattr(repos, "upsert_runtime_run", fail)
    with pytest.raises(sqlite3.OperationalError):
        coordinator.begin("session", run_id="failed")
    assert not coordinator.is_busy("session")
    monkeypatch.setattr(repos, "upsert_runtime_run", lambda *a, **k: {})
    token = coordinator.begin("session", run_id="retry")
    assert coordinator.can_commit(token)
