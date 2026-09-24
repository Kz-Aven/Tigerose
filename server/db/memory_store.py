"""Transactional memory operations. Callers own transaction boundaries."""

from __future__ import annotations

import hashlib
import json
import time
import uuid

from memory_core import MEMORY_TYPES, contains_secret, make_summary

_COLUMNS = {
    "type": "TEXT NOT NULL DEFAULT 'unknown'",
    "summary": "TEXT NOT NULL DEFAULT ''",
    "scope_kind": "TEXT NOT NULL DEFAULT 'assistant'",
    "scope_id": "TEXT NOT NULL DEFAULT ''",
    "version": "INTEGER NOT NULL DEFAULT 1",
    "updated_at": "REAL NOT NULL DEFAULT 0",
    "source_refs": "TEXT NOT NULL DEFAULT '[]'",
    "status": "TEXT NOT NULL DEFAULT 'active'",
}


class MemoryConflict(ValueError):
    def __init__(self, message="memory version conflict", current_version=None):
        super().__init__(message)
        self.current_version = current_version


def migrate(conn):
    checksum = hashlib.sha256(json.dumps(_COLUMNS, sort_keys=True).encode()).hexdigest()
    conn.execute("SAVEPOINT memory_v1")
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS memory_migrations (id TEXT PRIMARY KEY, checksum TEXT NOT NULL, ts REAL NOT NULL)")
        prior = conn.execute("SELECT checksum FROM memory_migrations WHERE id='structured_v1'").fetchone()
        if prior:
            if prior[0] != checksum:
                raise RuntimeError("memory migration checksum mismatch")
        else:
            columns = {r[1] for r in conn.execute("PRAGMA table_info(assistant_memories)")}
            for name, definition in _COLUMNS.items():
                if name not in columns:
                    conn.execute(f"ALTER TABLE assistant_memories ADD COLUMN {name} {definition}")
            for row in conn.execute("SELECT memory_id, body FROM assistant_memories").fetchall():
                conn.execute("UPDATE assistant_memories SET summary=?, scope_id=template_id, updated_at=ts WHERE memory_id=?", (make_summary(row[1]), row[0]))
            conn.execute("INSERT INTO memory_migrations VALUES ('structured_v1',?,?)", (checksum, time.time()))
        conn.execute("""CREATE TABLE IF NOT EXISTS memory_revisions (
            memory_id TEXT NOT NULL, version INTEGER NOT NULL, before_json TEXT,
            after_json TEXT NOT NULL, actor TEXT NOT NULL, ts REAL NOT NULL,
            PRIMARY KEY(memory_id,version),
            FOREIGN KEY(memory_id) REFERENCES assistant_memories(memory_id) ON DELETE CASCADE)""")
        conn.execute("CREATE TABLE IF NOT EXISTS memory_confirmations (template_id TEXT NOT NULL, source_key TEXT NOT NULL, memory_id TEXT NOT NULL, PRIMARY KEY(template_id,source_key))")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_scope ON assistant_memories(template_id,status,scope_kind,scope_id)")
        for row in conn.execute("SELECT * FROM assistant_memories WHERE memory_id NOT IN (SELECT memory_id FROM memory_revisions)").fetchall():
            _revision(conn, None, _row(row), "migration")
        conn.execute("RELEASE memory_v1")
    except Exception:
        conn.execute("ROLLBACK TO memory_v1")
        conn.execute("RELEASE memory_v1")
        raise


def _row(row):
    if row is None:
        return None
    result = dict(row)
    for field in ("source_groups", "source_refs"):
        result[field] = json.loads(result.get(field) or "[]")
    return result


def validate_scope(conn, template_id, scope_kind, scope_id):
    if scope_kind == "assistant" and scope_id == template_id:
        return
    if scope_kind == "group" and conn.execute(
        """SELECT 1 FROM group_memberships m JOIN project_groups g ON g.group_id=m.group_id
           WHERE m.template_id=? AND m.group_id=? AND g.status='active'""", (template_id, scope_id)
    ).fetchone():
        return
    raise ValueError("invalid or inaccessible memory scope")


def get(conn, template_id, memory_id, include_deleted=False):
    row = conn.execute("SELECT * FROM assistant_memories WHERE template_id=? AND memory_id=?", (template_id, memory_id)).fetchone()
    result = _row(row)
    return result if result and (include_deleted or result["status"] == "active") else None


def list_memories(conn, template_id, status="active", scope_kind=None, scope_id=None, type=None):
    clauses, args = ["template_id=?"], [template_id]
    for name, value in (("status", status), ("scope_kind", scope_kind), ("scope_id", scope_id), ("type", type)):
        if value is not None:
            clauses.append(f"{name}=?")
            args.append(value)
    return [_row(r) for r in conn.execute("SELECT * FROM assistant_memories WHERE " + " AND ".join(clauses) + " ORDER BY updated_at DESC,memory_id", args)]


def authorized(conn, template_id, group_id=None, index_only=False):
    args = [template_id, template_id]
    scope = "(scope_kind='assistant' AND scope_id=?)"
    if group_id:
        validate_scope(conn, template_id, "group", group_id)
        scope += " OR (scope_kind='group' AND scope_id=?)"
        args.append(group_id)
    columns = "memory_id,type,summary,scope_kind,scope_id,version,updated_at" if index_only else "*"
    rows = conn.execute(f"SELECT {columns} FROM assistant_memories WHERE template_id=? AND status='active' AND ({scope})", args)
    return [dict(r) if index_only else _row(r) for r in rows]


def _revision(conn, before, after, actor):
    conn.execute("INSERT INTO memory_revisions VALUES (?,?,?,?,?,?)", (
        after["memory_id"], after["version"], json.dumps(before, ensure_ascii=False) if before else None,
        json.dumps(after, ensure_ascii=False), actor, time.time()))


def create(conn, template_id, body, *, type="unknown", summary=None, scope_kind="assistant", scope_id=None,
           source="manual", source_refs=None, source_groups=None, actor="user"):
    body = body.strip()
    if not body or type not in MEMORY_TYPES:
        raise ValueError("invalid memory body or type")
    if contains_secret(body) or (summary and contains_secret(summary)):
        raise ValueError("memory contains credentials")
    scope_id = scope_id or template_id
    validate_scope(conn, template_id, scope_kind, scope_id)
    mid, now = "amem_" + uuid.uuid4().hex[:16], time.time()
    conn.execute("""INSERT INTO assistant_memories
        (memory_id,template_id,body,source_groups,source,ts,type,summary,scope_kind,scope_id,version,updated_at,source_refs,status)
        VALUES (?,?,?,?,?,?,?,?,?,?,1,?,?,'active')""", (
        mid, template_id, body, json.dumps(source_groups or []), source, now, type,
        make_summary(summary or body), scope_kind, scope_id, now, json.dumps(source_refs or [])))
    result = get(conn, template_id, mid)
    _revision(conn, None, result, actor)
    return result


def update(conn, template_id, memory_id, *, body=None, expected_version=None, actor="user", type=None, summary=None, status=None):
    before = get(conn, template_id, memory_id, include_deleted=True)
    if before is None:
        return None
    if expected_version is not None and before["version"] != expected_version:
        raise MemoryConflict(current_version=before["version"])
    if actor != "user" and expected_version is None:
        raise ValueError("expected_version required")
    if summary and contains_secret(summary):
        raise ValueError("memory contains credentials")
    if before["status"] != "active" and status == "active" and actor != "user":
        raise MemoryConflict("only explicit user action can restore memory", before["version"])
    if before["status"] == "deleted" and status != "active":
        raise MemoryConflict("memory is deleted", before["version"])
    after = dict(before)
    if body is not None:
        if not body.strip() or contains_secret(body):
            raise ValueError("invalid body or credentials")
        after["body"] = body.strip()
        after["summary"] = make_summary(summary or body)
    elif summary is not None:
        after["summary"] = make_summary(summary)
    if type is not None:
        if type not in MEMORY_TYPES:
            raise ValueError("invalid memory type")
        after["type"] = type
    if status is not None:
        if status not in {"active", "archived", "deleted"}:
            raise ValueError("invalid memory status")
        after["status"] = status
    after.update(version=before["version"] + 1, updated_at=time.time())
    cur = conn.execute("""UPDATE assistant_memories SET body=?,summary=?,type=?,status=?,version=?,updated_at=?
        WHERE template_id=? AND memory_id=? AND version=?""", tuple(after[k] for k in ("body", "summary", "type", "status", "version", "updated_at")) + (template_id, memory_id, before["version"]))
    if cur.rowcount != 1:
        raise MemoryConflict()
    _revision(conn, before, after, actor)
    return after


def revisions(conn, template_id, memory_id):
    if not get(conn, template_id, memory_id, include_deleted=True):
        return []
    return [{"memory_id": r["memory_id"], "version": r["version"], "before": json.loads(r["before_json"]) if r["before_json"] else None,
             "after": json.loads(r["after_json"]), "actor": r["actor"], "ts": r["ts"]}
            for r in conn.execute("SELECT * FROM memory_revisions WHERE memory_id=? ORDER BY version DESC", (memory_id,))]


def restore(conn, template_id, memory_id, revision_version, expected_version):
    revision = next((r for r in revisions(conn, template_id, memory_id) if r["version"] == revision_version), None)
    if not revision:
        return None
    value = revision["after"]
    validate_scope(conn, template_id, value["scope_kind"], value["scope_id"])
    return update(conn, template_id, memory_id, body=value["body"], type=value["type"], summary=value["summary"], status="active", expected_version=expected_version)


def _proposal_origin(conn, template_id, message_id, event_id, group_id):
    if message_id and event_id:
        raise ValueError("choose one proposal source")
    origin = None
    if message_id:
        origin = conn.execute("SELECT * FROM assistant_messages WHERE template_id=? AND message_id=? AND role='assistant'", (template_id, message_id)).fetchone()
    elif event_id:
        validate_scope(conn, template_id, "group", group_id)
        origin = conn.execute("""SELECT e.* FROM feed_events e JOIN group_memberships m ON m.instance_id=e.speaker_id
            WHERE e.event_id=? AND e.group_id=? AND m.template_id=? AND m.group_id=e.group_id""", (event_id, group_id, template_id)).fetchone()
    if (message_id or event_id) and not origin:
        raise LookupError("proposal source not found")
    return origin


def dismiss(conn, template_id, *, message_id=None, event_id=None, group_id=None):
    if not message_id and not event_id:
        raise ValueError("message_id or event_id required")
    origin = _proposal_origin(conn, template_id, message_id, event_id, group_id)
    meta = json.loads(origin["meta"] or "{}")
    if not meta.get("remember_proposal"):
        raise ValueError("source has no memory proposal")
    if meta.get("remember_confirmed"):
        raise MemoryConflict("proposal was already confirmed")
    meta["remember_ignored"] = True
    table, key = ("assistant_messages", "message_id") if message_id else ("feed_events", "event_id")
    conn.execute(f"UPDATE {table} SET meta=? WHERE {key}=?", (json.dumps(meta, ensure_ascii=False), message_id or event_id))


def confirm(conn, template_id, body, *, message_id=None, event_id=None, group_id=None, idempotency_key=None, **kwargs):
    origin = _proposal_origin(conn, template_id, message_id, event_id, group_id)
    source_key = ("message:" + message_id) if message_id else ("event:" + event_id) if event_id else ("client:" + idempotency_key) if idempotency_key else None
    if source_key:
        previous = conn.execute("SELECT memory_id FROM memory_confirmations WHERE template_id=? AND source_key=?", (template_id, source_key)).fetchone()
        if previous:
            return get(conn, template_id, previous[0], include_deleted=True)
    meta = json.loads(origin["meta"] or "{}") if origin else {}
    if meta.get("remember_confirmed"):
        raise MemoryConflict("proposal was already confirmed")
    if origin and (not meta.get("remember_proposal") or meta.get("remember_ignored")):
        raise ValueError("source has no pending memory proposal")
    kwargs["source_refs"] = [{"kind": "message" if message_id else "event", "id": message_id or event_id}] if origin else []
    if event_id:
        kwargs.update(scope_kind="group", scope_id=group_id)
    result = create(conn, template_id, body, **kwargs)
    if origin:
        meta["remember_confirmed"] = True
        meta["memory_id"] = result["memory_id"]
        table, key = ("assistant_messages", "message_id") if message_id else ("feed_events", "event_id")
        conn.execute(f"UPDATE {table} SET meta=? WHERE {key}=?", (json.dumps(meta, ensure_ascii=False), message_id or event_id))
    if source_key:
        conn.execute("INSERT INTO memory_confirmations VALUES (?,?,?)", (template_id, source_key, result["memory_id"]))
    return result
