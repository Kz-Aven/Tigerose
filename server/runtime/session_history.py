"""Durable source pointers for model history; message bodies stay in their stores."""

from __future__ import annotations

import json
import time
from contextlib import contextmanager

from server.db.connection import get_connection


def migrate(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS session_fences (
        session_id TEXT PRIMARY KEY, epoch INTEGER NOT NULL DEFAULT 0, deleted_at REAL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS session_history_events (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL, session_epoch INTEGER NOT NULL,
        source_kind TEXT NOT NULL, source_id TEXT NOT NULL,
        run_id TEXT NOT NULL DEFAULT '', instance_id TEXT NOT NULL DEFAULT '',
        UNIQUE(session_id, session_epoch, source_kind, source_id))""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_history_session ON session_history_events(session_id, session_epoch, seq)")


def current_epoch(conn, session_id):
    conn.execute("INSERT OR IGNORE INTO session_fences(session_id) VALUES (?)", (session_id,))
    row = conn.execute("SELECT epoch, deleted_at FROM session_fences WHERE session_id=?", (session_id,)).fetchone()
    if row[1] is not None:
        raise RuntimeError("session has been deleted")
    return int(row[0])


def record(conn, session_id, kind, source_id, *, run_id="", instance_id="", expected_epoch=None):
    if not session_id:
        return
    epoch = current_epoch(conn, session_id)
    if expected_epoch is not None and epoch != expected_epoch:
        raise RuntimeError("session generation changed")
    conn.execute(
        """INSERT OR IGNORE INTO session_history_events
        (session_id, session_epoch, source_kind, source_id, run_id, instance_id)
        VALUES (?, ?, ?, ?, ?, ?)""",
        (session_id, epoch, kind, source_id, run_id, instance_id),
    )


@contextmanager
def commit_guard(state, conn=None):
    """Serialize file commits with durable clear/delete fences in SQLite."""
    if conn is not None:
        epoch = current_epoch(conn, state.meta.session_id)
        if epoch != state.context.get("session_epoch", epoch):
            raise RuntimeError("session generation changed")
        yield conn
        return
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        epoch = current_epoch(conn, state.meta.session_id)
        if epoch != state.context.get("session_epoch", epoch):
            raise RuntimeError("session generation changed")
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def invalidate(session_id, *, deleted=False, on_invalidate=None):
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        epoch = current_epoch(conn, session_id) + 1
        conn.execute("UPDATE session_fences SET epoch=?, deleted_at=? WHERE session_id=?",
                     (epoch, time.time() if deleted else None, session_id))
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "pending_questions" in tables:
            conn.execute("UPDATE pending_questions SET status='cancelled' WHERE session_id=? AND status='pending'", (session_id,))
        if "memory_jobs" in tables:
            conn.execute("UPDATE memory_jobs SET status='cancelled' WHERE session_id=? AND status IN ('pending','running','failed')", (session_id,))
        if "memory_candidates" in tables:
            # Worker/confirmation also check the fence, even if an older schema has no session column.
            columns = {r[1] for r in conn.execute("PRAGMA table_info(memory_candidates)")}
            if "session_id" in columns:
                conn.execute("UPDATE memory_candidates SET status='cancelled' WHERE session_id=? AND status='pending'", (session_id,))
            elif "job_id" in columns and "memory_jobs" in tables:
                conn.execute("UPDATE memory_candidates SET status='cancelled' WHERE status='pending' AND job_id IN (SELECT job_id FROM memory_jobs WHERE session_id=?)", (session_id,))
        if on_invalidate is not None:
            on_invalidate(conn, epoch)
        conn.commit()
        return epoch
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def project(state):
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        epoch = current_epoch(conn, state.meta.session_id)
        if state.context.get("session_epoch", epoch) != epoch:
            raise RuntimeError("session generation changed")
        state.context["session_epoch"] = epoch
        source_times = _backfill_original_sources(conn, state) if epoch == 0 and "history_cursor" not in state.context else {}
        cursor = int(state.context.get("history_cursor", 0))
        rows = conn.execute(
            "SELECT * FROM session_history_events WHERE session_id=? AND session_epoch=? AND seq>? ORDER BY seq",
            (state.meta.session_id, epoch, cursor),
        ).fetchall()
        if source_times:
            rows.sort(key=lambda row: (source_times.get((row["source_kind"], row["source_id"]), float("inf")), row["seq"]))
        for event in rows:
            kind, source_id = event["source_kind"], event["source_id"]
            content, role, meta = "", "", {}
            if kind == "message":
                row = conn.execute("SELECT role,content,meta,template_id FROM assistant_messages WHERE message_id=? AND session_id=?",
                                   (source_id, state.meta.session_id)).fetchone()
                if row:
                    if not state.meta.scope_key.endswith(f":assistant:{row[3]}"):
                        raise RuntimeError("history source belongs to another assistant")
                    role, content, meta = row[0], row[1], json.loads(row[2])
            elif kind == "question":
                row = conn.execute("SELECT * FROM pending_questions WHERE question_id=? AND session_id=? AND status='answered'",
                                   (source_id, state.meta.session_id)).fetchone()
                if row:
                    from server.runtime.ask_user_transcript import answer_content
                    content = answer_content({"payload": json.loads(row["payload"])}, json.loads(row["answer"])["answers"])
                    role = "user"
            elif kind == "feed":
                row = conn.execute("SELECT content,speaker_type,meta,group_id,speaker_id FROM feed_events WHERE event_id=?", (source_id,)).fetchone()
                if row:
                    meta = json.loads(row[2])
                    expected_scope = f"group:{row[3]}:agent:{event['instance_id']}"
                    if state.meta.scope_key != expected_scope or (row[1] == "agent" and row[4] != event["instance_id"]):
                        raise RuntimeError("history feed source belongs to another agent")
                    is_goal_input = row[1] == "system" and meta.get("goal_command") in ("set", "resume")
                    if row[1] not in ("user", "agent") and not is_goal_input:
                        raise RuntimeError("history feed source is not dialogue")
                    content, role = row[0], "user" if row[1] == "user" or is_goal_input else "assistant"
            if not role:
                raise RuntimeError(f"history source missing: {kind}:{source_id}")
            if meta.get("attachments"):
                from server.runtime.turn import format_message_with_attachments
                content = format_message_with_attachments(content, meta["attachments"])
            existing = next((m for m in state.messages if
                (m.get("source_kind") == kind and m.get("source_id") == source_id)
                or (kind == "question" and m.get("question_id") == source_id)
                or (role == "assistant" and not meta.get("intermediate") and event["run_id"]
                    and m.get("run_id") == event["run_id"] and m.get("role") == role
                    and not m.get("intermediate"))), None)
            message = {"role": role, "content": content, "source_kind": kind,
                       "source_id": source_id, "run_id": event["run_id"]}
            if kind == "question":
                message["question_id"] = source_id
            if meta.get("intermediate"):
                message["intermediate"] = True
            if existing is not None:
                existing.update(message)
            else:
                final_index = next((i for i, item in enumerate(state.messages)
                                    if meta.get("intermediate") and event["run_id"]
                                    and item.get("run_id") == event["run_id"]
                                    and item.get("role") == "assistant" and not item.get("intermediate")), len(state.messages))
                state.messages.insert(final_index, message)
            cursor = max(cursor, event["seq"])
        state.context["history_cursor"] = cursor
        conn.commit()
    finally:
        conn.close()


def _backfill_original_sources(conn, state):
    """One-time legacy repair from original records, never from assistant guesses."""
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    sources = []
    if "assistant_messages" in tables and ":assistant:" in state.meta.scope_key:
        owner = state.meta.scope_key.rsplit(":assistant:", 1)[1]
        rows = conn.execute("SELECT message_id,role,meta,ts FROM assistant_messages WHERE session_id=? AND template_id=? ORDER BY ts,message_id",
                            (state.meta.session_id, owner))
        for row in rows:
            meta = json.loads(row[2])
            if row[1] not in ("user", "assistant") or meta.get("ask_user") or meta.get("ask_user_answer"):
                continue
            sources.append((row[3], "message", row[0], meta.get("run_id", ""), ""))
    if "pending_questions" in tables:
        rows = conn.execute("SELECT question_id,run_id,instance_id,answered_at FROM pending_questions WHERE session_id=? AND status='answered' AND answer IS NOT NULL",
                            (state.meta.session_id,))
        for row in rows:
            sources.append((row[3] or 0, "question", row[0], row[1], row[2]))
    for _, kind, source_id, run_id, instance_id in sorted(sources):
        record(conn, state.meta.session_id, kind, source_id, run_id=run_id, instance_id=instance_id, expected_epoch=0)
    return {(kind, source_id): ts for ts, kind, source_id, _, _ in sources}


def received_run(state, run_id):
    return bool(run_id) and any(m.get("role") == "user" and m.get("run_id") == run_id for m in state.messages)
