"""Data access for Project Group collaboration."""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from .connection import get_connection


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _now() -> float:
    return time.time()


def _json(val: Any) -> str:
    return json.dumps(val, ensure_ascii=False)


def _loads(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return default


def row_to_dict(row) -> dict:
    return dict(row) if row else {}


# ── Templates ──────────────────────────────────────────────


def _hydrate_template(d: dict) -> dict:
    from server.capabilities.catalog import normalize_capabilities

    d["tools_allowlist"] = _loads(d.get("tools_allowlist"), [])
    d["config_meta"] = _loads(d.get("config_meta"), {})
    caps = normalize_capabilities(_loads(d.get("capabilities"), {}))
    # Mirror canonical tools into tools_allowlist for read compat
    if caps.get("tools"):
        d["tools_allowlist"] = list(caps["tools"])
    elif d["tools_allowlist"] and not caps.get("tools"):
        caps["tools"] = list(d["tools_allowlist"])
    d["capabilities"] = caps
    return d


def list_templates() -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM agent_templates ORDER BY created_at ASC"
        ).fetchall()
        return [_hydrate_template(row_to_dict(r)) for r in rows]
    finally:
        conn.close()


def get_template(template_id: str) -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM agent_templates WHERE template_id = ?", (template_id,)
        ).fetchone()
        if not row:
            return None
        return _hydrate_template(row_to_dict(row))
    finally:
        conn.close()


def create_template(
    *,
    name: str,
    role: str = "",
    system_prompt: str = "",
    tools_allowlist: list | None = None,
    capabilities: dict | None = None,
    theme_color: str = "#5db8a6",
    avatar_id: str = "",
    model_profile_id: str = "",
    config_meta: dict | None = None,
    template_id: str | None = None,
) -> dict:
    from server.capabilities.catalog import normalize_capabilities

    tid = template_id or _id("tpl")
    now = _now()
    caps = normalize_capabilities(capabilities or {})
    if tools_allowlist and not caps.get("tools"):
        caps["tools"] = list(tools_allowlist)
    # Keep legacy column in sync for older readers
    tools_mirror = list(caps.get("tools") or tools_allowlist or [])
    conn = get_connection()
    try:
        conn.execute(
            """INSERT INTO agent_templates
               (template_id, name, role, system_prompt, tools_allowlist, capabilities,
                theme_color, avatar_id, model_profile_id, config_meta, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                tid,
                name,
                role,
                system_prompt,
                _json(tools_mirror),
                _json(caps),
                theme_color,
                avatar_id,
                model_profile_id or "",
                _json(config_meta or {}),
                now,
                now,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return get_template(tid)  # type: ignore


def update_template(template_id: str, **fields) -> dict | None:
    from server.capabilities.catalog import normalize_capabilities

    allowed = {
        "name",
        "role",
        "system_prompt",
        "theme_color",
        "avatar_id",
        "model_profile_id",
        "config_meta",
        "capabilities",
    }
    # tools_allowlist writes rejected at API; ignore if somehow passed
    fields.pop("tools_allowlist", None)
    updates = []
    values = []
    for k, v in fields.items():
        if k not in allowed or v is None:
            continue
        if k == "config_meta":
            v = _json(v)
        elif k == "capabilities":
            caps = normalize_capabilities(v)
            values.append(_json(caps))
            updates.append("capabilities = ?")
            # mirror tools into legacy column
            updates.append("tools_allowlist = ?")
            values.append(_json(caps.get("tools") or []))
            continue
        updates.append(f"{k} = ?")
        values.append(v)
    if not updates:
        return get_template(template_id)
    updates.append("updated_at = ?")
    values.append(_now())
    values.append(template_id)
    conn = get_connection()
    try:
        conn.execute(
            f"UPDATE agent_templates SET {', '.join(updates)} WHERE template_id = ?",
            values,
        )
        conn.commit()
    finally:
        conn.close()
    return get_template(template_id)


def list_capability_registry() -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM capability_registry ORDER BY capability_id ASC"
        ).fetchall()
        out = []
        for r in rows:
            d = row_to_dict(r)
            d["meta"] = _loads(d.get("meta"), {})
            out.append(d)
        return out
    finally:
        conn.close()


def upsert_capability_registry(
    capability_id: str,
    *,
    kind: str,
    source_path: str = "",
    display_name: str = "",
    description: str = "",
    meta: dict | None = None,
    hidden: bool = False,
) -> dict:
    now = _now()
    conn = get_connection()
    try:
        conn.execute(
            """INSERT INTO capability_registry
               (capability_id, kind, source_path, display_name, description, meta, hidden, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(capability_id) DO UPDATE SET
                 kind=excluded.kind,
                 source_path=excluded.source_path,
                 display_name=excluded.display_name,
                 description=excluded.description,
                 meta=excluded.meta,
                 hidden=excluded.hidden,
                 updated_at=excluded.updated_at
            """,
            (
                capability_id,
                kind,
                source_path,
                display_name,
                description,
                _json(meta or {}),
                1 if hidden else 0,
                now,
            ),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM capability_registry WHERE capability_id = ?",
            (capability_id,),
        ).fetchone()
        d = row_to_dict(row)
        d["meta"] = _loads(d.get("meta"), {})
        return d
    finally:
        conn.close()


def delete_capability_registry(capability_id: str) -> bool:
    conn = get_connection()
    try:
        cur = conn.execute(
            "DELETE FROM capability_registry WHERE capability_id = ?",
            (capability_id,),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def strip_capability_from_all_templates(capability_id: str) -> int:
    """Remove a capability id (and bare name / tool name) from all templates."""
    bare = capability_id.split(":", 1)[-1] if ":" in capability_id else capability_id
    kind = capability_id.split(":", 1)[0] if ":" in capability_id else ""
    key_map = {
        "skill": "skills",
        "plugin": "plugins",
        "tool": "tools",
        "mcp": "mcp_servers",
    }
    field = key_map.get(kind)
    changed = 0
    for tpl in list_templates():
        caps = dict(tpl.get("capabilities") or {})
        if not field:
            # try all lists
            fields = list(key_map.values())
        else:
            fields = [field]
        dirty = False
        for f in fields:
            arr = list(caps.get(f) or [])
            new_arr = [
                x
                for x in arr
                if x != capability_id
                and x != bare
                and x != f"tool:{bare}"
                and x != f"mcp:{bare}"
                and x != f"skill:{bare}"
            ]
            if len(new_arr) != len(arr):
                caps[f] = new_arr
                dirty = True
        if dirty:
            update_template(tpl["template_id"], capabilities=caps)
            changed += 1
    return changed


def delete_template(template_id: str) -> bool:
    conn = get_connection()
    try:
        conn.execute('BEGIN IMMEDIATE')
        active_mesh = conn.execute(
            "SELECT 1 FROM workflow_tasks t JOIN mesh_task_bindings b USING(task_id) "
            "WHERE (b.caller_id=? OR b.target_id=?) AND t.status NOT IN ('completed','failed','cancelled','timeout') LIMIT 1",
            (template_id, template_id),
        ).fetchone()
        if active_mesh:
            return False
        used = conn.execute(
            "SELECT 1 FROM group_memberships WHERE template_id = ? LIMIT 1",
            (template_id,),
        ).fetchone()
        if used:
            return False
        conn.execute("DELETE FROM agent_templates WHERE template_id = ?", (template_id,))
        conn.commit()
        return True
    finally:
        conn.close()


def template_in_use(template_id: str) -> bool:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT 1 FROM group_memberships WHERE template_id = ? LIMIT 1",
            (template_id,),
        ).fetchone()
        return bool(row)
    finally:
        conn.close()


# ── Assistant messages / memories ─────────────────────────


def list_assistant_messages(template_id: str, limit: int = 200, *, session_id: str | None = None) -> list[dict]:
    """Latest `limit` messages, chronological (oldest→newest within page)."""
    if session_id is None:
        # Legacy: latest across all sessions (sidebar preview)
        page = list_assistant_messages_page(template_id, limit=limit, session_id="")
        return page["items"]
    page = list_assistant_messages_page(template_id, limit=limit, session_id=session_id)
    return page["items"]


def _preview_snippet(text: str, max_len: int = 40) -> str:
    t = " ".join((text or "").split())
    if not t:
        return ""
    if len(t) <= max_len:
        return t
    return t[: max_len - 1] + "…"


def latest_assistant_message_preview(template_id: str, max_len: int = 40) -> str:
    page = list_assistant_messages_page(template_id, limit=1, session_id="")
    items = page.get("items") or []
    if not items:
        return ""
    return _preview_snippet(str(items[-1].get("content") or ""), max_len=max_len)


def latest_feed_preview(group_id: str, max_len: int = 40) -> str:
    page = list_feed_page(group_id, limit=1)
    items = page.get("items") or []
    if not items:
        return ""
    return _preview_snippet(str(items[-1].get("content") or ""), max_len=max_len)


def list_assistant_messages_page(
    template_id: str,
    *,
    session_id: str = "",
    limit: int = 10,
    before_ts: float | None = None,
) -> dict:
    """Return up to `limit` messages ending just before before_ts (or latest if None).

    session_id='' means all sessions for this template (migration / preview).
    """
    limit = max(1, min(int(limit), 100))
    conn = get_connection()
    try:
        if session_id:
            where = "template_id = ? AND session_id = ?"
            base_args: list = [template_id, session_id]
        else:
            where = "template_id = ?"
            base_args = [template_id]
        if before_ts is None:
            rows = conn.execute(
                f"""SELECT * FROM (
                     SELECT * FROM assistant_messages
                     WHERE {where}
                     ORDER BY ts DESC LIMIT ?
                   ) sub ORDER BY ts ASC""",
                (*base_args, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                f"""SELECT * FROM (
                     SELECT * FROM assistant_messages
                     WHERE {where} AND ts < ?
                     ORDER BY ts DESC LIMIT ?
                   ) sub ORDER BY ts ASC""",
                (*base_args, before_ts, limit),
            ).fetchall()
        items = []
        for r in rows:
            d = row_to_dict(r)
            d["meta"] = _loads(d.get("meta"), {})
            items.append(d)
        has_more = False
        if items:
            oldest_ts = items[0]["ts"]
            older = conn.execute(
                f"""SELECT 1 FROM assistant_messages
                   WHERE {where} AND ts < ?
                   LIMIT 1""",
                (*base_args, oldest_ts),
            ).fetchone()
            has_more = bool(older)
        return {"items": items, "has_more": has_more, "limit": limit}
    finally:
        conn.close()


def export_assistant_messages_for_session(
    template_id: str, session_id: str, *, limit: int = 50000
) -> list[dict]:
    """Full chronological export for archival (not UI paging)."""
    limit = max(1, min(int(limit), 100000))
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT * FROM assistant_messages
               WHERE template_id = ? AND session_id = ?
               ORDER BY ts ASC LIMIT ?""",
            (template_id, session_id, limit),
        ).fetchall()
        items = []
        for r in rows:
            d = row_to_dict(r)
            d["meta"] = _loads(d.get("meta"), {})
            items.append(d)
        return items
    finally:
        conn.close()


def add_assistant_message(
    template_id: str,
    role: str,
    content: str,
    *,
    session_id: str = "",
    meta: dict | None = None,
) -> dict:
    mid = _id("amsg")
    ts = _now()
    meta = meta or {}
    sid = (session_id or "").strip()
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        runs = meta.get("runs") or []
        rid = str(meta.get("run_id") or (runs[0].get("run_id") if runs else "") or "")
        if sid and rid:
            from server.runtime.run_fence import validate
            validate(conn, rid, sid)
        conn.execute(
            """INSERT INTO assistant_messages
               (message_id, template_id, role, content, ts, meta, session_id)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (mid, template_id, role, content, ts, _json(meta), sid),
        )
        if sid and role in {"user", "assistant"} and not meta.get("ask_user") and not meta.get("ask_user_answer"):
            from server.runtime.session_history import record

            runs = meta.get("runs") or []
            rid = str(meta.get("run_id") or (runs[0].get("run_id") if runs else "") or "")
            record(conn, sid, "message", mid, run_id=rid, expected_epoch=meta.get("session_epoch"))
        conn.commit()
    finally:
        conn.close()
    return {
        "message_id": mid,
        "template_id": template_id,
        "role": role,
        "content": content,
        "ts": ts,
        "meta": meta,
        "session_id": sid,
    }


def clear_assistant_messages(template_id: str, session_id: str) -> int:
    conn = get_connection()
    try:
        cur = conn.execute(
            "DELETE FROM assistant_messages WHERE template_id = ? AND session_id = ?",
            (template_id, session_id),
        )
        conn.commit()
        return int(cur.rowcount or 0)
    finally:
        conn.close()


def delete_assistant_messages_for_session(template_id: str, session_id: str) -> int:
    return clear_assistant_messages(template_id, session_id)


def delete_all_assistant_messages(template_id: str) -> int:
    conn = get_connection()
    try:
        cur = conn.execute(
            "DELETE FROM assistant_messages WHERE template_id = ?",
            (template_id,),
        )
        conn.commit()
        return int(cur.rowcount or 0)
    finally:
        conn.close()


def count_assistant_messages(template_id: str, session_id: str) -> int:
    conn = get_connection()
    try:
        row = conn.execute(
            """SELECT COUNT(*) AS n FROM assistant_messages
               WHERE template_id = ? AND session_id = ?""",
            (template_id, session_id),
        ).fetchone()
        return int(row[0] if row else 0)
    finally:
        conn.close()


def count_messages_missing_session(template_id: str) -> int:
    conn = get_connection()
    try:
        row = conn.execute(
            """SELECT COUNT(*) AS n FROM assistant_messages
               WHERE template_id = ? AND (session_id IS NULL OR session_id = '')""",
            (template_id,),
        ).fetchone()
        return int(row[0] if row else 0)
    finally:
        conn.close()


def latest_message_ts(template_id: str, session_id: str) -> float | None:
    conn = get_connection()
    try:
        row = conn.execute(
            """SELECT ts FROM assistant_messages
               WHERE template_id = ? AND session_id = ?
               ORDER BY ts DESC LIMIT 1""",
            (template_id, session_id),
        ).fetchone()
        return float(row[0]) if row else None
    finally:
        conn.close()


def first_user_message_content(template_id: str, session_id: str) -> str:
    conn = get_connection()
    try:
        row = conn.execute(
            """SELECT content FROM assistant_messages
               WHERE template_id = ? AND session_id = ? AND role = 'user'
               ORDER BY ts ASC LIMIT 1""",
            (template_id, session_id),
        ).fetchone()
        return str(row[0]) if row else ""
    finally:
        conn.close()


def backfill_null_session_ids(template_id: str, session_id: str) -> int:
    conn = get_connection()
    try:
        cur = conn.execute(
            """UPDATE assistant_messages SET session_id = ?
               WHERE template_id = ? AND (session_id IS NULL OR session_id = '')""",
            (session_id, template_id),
        )
        conn.commit()
        return int(cur.rowcount or 0)
    finally:
        conn.close()


def _normalize_memory_source(source: str | None) -> str:
    s = (source or "manual").strip().lower()
    if s not in {"tool", "confirm", "manual"}:
        return "manual"
    return s


def _memory_row(d: dict) -> dict:
    d["source_groups"] = _loads(d.get("source_groups"), [])
    d["source"] = _normalize_memory_source(d.get("source"))
    return d


def list_assistant_memories(template_id: str, **filters) -> list[dict]:
    from . import memory_store
    conn = get_connection()
    try:
        return memory_store.list_memories(conn, template_id, **filters)
    finally:
        conn.close()


def add_assistant_memory(
    template_id: str,
    body: str,
    *,
    source_groups: list | None = None,
    source: str = "manual",
    **fields,
) -> dict:
    from . import memory_store
    conn = get_connection()
    try:
        with conn:
            return memory_store.create(conn, template_id, body, source_groups=source_groups,
                                       source=_normalize_memory_source(source), **fields)
    finally:
        conn.close()


def get_assistant_memory(template_id: str, memory_id: str) -> dict | None:
    from . import memory_store
    conn = get_connection()
    try:
        return memory_store.get(conn, template_id, memory_id)
    finally:
        conn.close()


def update_assistant_memory(template_id: str, memory_id: str, body: str, **fields) -> dict | None:
    from . import memory_store
    body = (body or "").strip()
    if not body:
        return None
    conn = get_connection()
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            return memory_store.update(conn, template_id, memory_id, body=body, **fields)
    finally:
        conn.close()


def delete_assistant_memory(template_id: str, memory_id: str, expected_version=None) -> bool:
    from . import memory_store
    conn = get_connection()
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            return memory_store.update(conn, template_id, memory_id, status="deleted", expected_version=expected_version) is not None
    finally:
        conn.close()


def get_assistant_message(template_id: str, message_id: str) -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute(
            """SELECT * FROM assistant_messages
               WHERE template_id = ? AND message_id = ?""",
            (template_id, message_id),
        ).fetchone()
        if not row:
            return None
        d = row_to_dict(row)
        d["meta"] = _loads(d.get("meta"), {})
        return d
    finally:
        conn.close()


def patch_assistant_message_meta(
    template_id: str, message_id: str, patch: dict
) -> dict | None:
    msg = get_assistant_message(template_id, message_id)
    if not msg:
        return None
    meta = dict(msg.get("meta") or {})
    meta.update(patch or {})
    conn = get_connection()
    try:
        conn.execute(
            """UPDATE assistant_messages SET meta = ?
               WHERE template_id = ? AND message_id = ?""",
            (_json(meta), template_id, message_id),
        )
        conn.commit()
    finally:
        conn.close()
    msg["meta"] = meta
    return msg


def find_assistant_message_for_ask_user(
    template_id: str,
    session_id: str,
    question_id: str,
    *,
    limit: int = 100,
) -> dict | None:
    """Locate the interrupt transcript message for a pending question."""
    qid = (question_id or "").strip()
    if not qid:
        return None
    page = list_assistant_messages_page(
        template_id,
        session_id=session_id or "",
        limit=limit,
    )
    for msg in reversed(page.get("items") or []):
        meta = msg.get("meta") or {}
        ask = meta.get("ask_user") if isinstance(meta, dict) else None
        if isinstance(ask, dict) and str(ask.get("question_id") or "") == qid:
            return msg
    return None


def find_feed_event_for_ask_user(
    group_id: str,
    question_id: str,
    *,
    limit: int = 100,
) -> dict | None:
    qid = (question_id or "").strip()
    if not qid:
        return None
    page = list_feed_page(group_id, limit=limit)
    for event in reversed(page.get("items") or []):
        meta = event.get("meta") or {}
        ask = meta.get("ask_user") if isinstance(meta, dict) else None
        if isinstance(ask, dict) and str(ask.get("question_id") or "") == qid:
            return event
    return None


def get_feed_event(group_id: str, event_id: str) -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute(
            """SELECT * FROM feed_events
               WHERE group_id = ? AND event_id = ?""",
            (group_id, event_id),
        ).fetchone()
        if not row:
            return None
        d = row_to_dict(row)
        d["meta"] = _loads(d.get("meta"), {})
        return d
    finally:
        conn.close()


def patch_feed_event_meta(group_id: str, event_id: str, patch: dict) -> dict | None:
    event = get_feed_event(group_id, event_id)
    if not event:
        return None
    meta = dict(event.get("meta") or {})
    meta.update(patch or {})
    conn = get_connection()
    try:
        conn.execute(
            """UPDATE feed_events SET meta = ?
               WHERE group_id = ? AND event_id = ?""",
            (_json(meta), group_id, event_id),
        )
        conn.commit()
    finally:
        conn.close()
    event["meta"] = meta
    return event


# ── Groups ────────────────────────────────────────────────


def list_groups() -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM project_groups WHERE status != 'deleted' ORDER BY created_at ASC"
        ).fetchall()
        out = []
        for r in rows:
            d = row_to_dict(r)
            d["settings"] = _loads(d.get("settings"), {})
            out.append(d)
        return out
    finally:
        conn.close()


def get_group(group_id: str) -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM project_groups WHERE group_id = ?", (group_id,)
        ).fetchone()
        if not row:
            return None
        d = row_to_dict(row)
        d["settings"] = _loads(d.get("settings"), {})
        return d
    finally:
        conn.close()


def create_group(
    *,
    name: str,
    workspace_path: str = "",
    settings: dict | None = None,
) -> dict:
    gid = _id("grp")
    now = _now()
    conn = get_connection()
    try:
        conn.execute(
            """INSERT INTO project_groups
               (group_id, name, workspace_path, status, settings, created_at, updated_at)
               VALUES (?, ?, ?, 'active', ?, ?, ?)""",
            (gid, name, workspace_path, _json(settings or {}), now, now),
        )
        conn.commit()
    finally:
        conn.close()
    return get_group(gid)  # type: ignore


def update_group(group_id: str, **fields) -> dict | None:
    allowed = {"name", "workspace_path", "status", "settings"}
    updates, values = [], []
    for k, v in fields.items():
        if k not in allowed or v is None:
            continue
        if k == "settings":
            v = _json(v)
        updates.append(f"{k} = ?")
        values.append(v)
    if not updates:
        return get_group(group_id)
    updates.append("updated_at = ?")
    values.append(_now())
    values.append(group_id)
    conn = get_connection()
    try:
        conn.execute(
            f"UPDATE project_groups SET {', '.join(updates)} WHERE group_id = ?",
            values,
        )
        conn.commit()
    finally:
        conn.close()
    return get_group(group_id)


def clear_feed(group_id: str) -> int:
    conn = get_connection()
    try:
        cur = conn.execute("DELETE FROM feed_events WHERE group_id = ?", (group_id,))
        conn.commit()
        return int(cur.rowcount or 0)
    finally:
        conn.close()


def soft_delete_group(group_id: str) -> bool:
    """Soft-delete group: status=deleted, wipe feed + memberships + related tasks."""
    g = get_group(group_id)
    if not g or g.get("status") == "deleted":
        return False
    conn = get_connection()
    try:
        now = _now()
        conn.execute(
            "UPDATE project_groups SET status = 'deleted', updated_at = ? WHERE group_id = ?",
            (now, group_id),
        )
        conn.execute("DELETE FROM feed_events WHERE group_id = ?", (group_id,))
        conn.execute("DELETE FROM group_memberships WHERE group_id = ?", (group_id,))
        try:
            conn.execute(
                "DELETE FROM runtime_tasks WHERE scope_key LIKE ?",
                (f"group:{group_id}:%",),
            )
        except Exception:
            pass
        conn.commit()
        return True
    finally:
        conn.close()


# ── Memberships ───────────────────────────────────────────


def list_members(group_id: str) -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT m.*, COALESCE(t.avatar_id, '') AS avatar_id
               FROM group_memberships m
               LEFT JOIN agent_templates t ON t.template_id = m.template_id
               WHERE m.group_id = ? ORDER BY m.created_at ASC""",
            (group_id,),
        ).fetchall()
        return [row_to_dict(r) for r in rows]
    finally:
        conn.close()


def get_member(instance_id: str) -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute(
            """SELECT m.*, COALESCE(t.avatar_id, '') AS avatar_id
               FROM group_memberships m
               LEFT JOIN agent_templates t ON t.template_id = m.template_id
               WHERE m.instance_id = ?""",
            (instance_id,),
        ).fetchone()
        return row_to_dict(row) if row else None
    finally:
        conn.close()


def add_member(
    group_id: str,
    template_id: str,
    *,
    display_name: str | None = None,
    theme_color: str | None = None,
    is_coordinator: bool = False,
) -> dict:
    tpl = get_template(template_id)
    if not tpl:
        raise ValueError("template not found")
    iid = _id("inst")
    now = _now()
    conn = get_connection()
    try:
        conn.execute(
            """INSERT INTO group_memberships
               (instance_id, group_id, template_id, display_name, theme_color,
                is_coordinator, runtime_status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, 'idle', ?)""",
            (
                iid,
                group_id,
                template_id,
                display_name or tpl["name"],
                theme_color or tpl["theme_color"],
                1 if is_coordinator else 0,
                now,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return get_member(iid)  # type: ignore


def remove_member(group_id: str, instance_id: str) -> bool:
    conn = get_connection()
    try:
        cur = conn.execute(
            """UPDATE group_memberships SET runtime_status = 'offline'
               WHERE group_id = ? AND instance_id = ?""",
            (group_id, instance_id),
        )
        conn.execute(
            "DELETE FROM group_memberships WHERE group_id = ? AND instance_id = ?",
            (group_id, instance_id),
        )
        conn.commit()
        return cur.rowcount > 0 or True
    finally:
        conn.close()


def set_member_status(instance_id: str, status: str) -> None:
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE group_memberships SET runtime_status = ? WHERE instance_id = ?",
            (status, instance_id),
        )
        conn.commit()
    finally:
        conn.close()


def find_members_by_mention(group_id: str, names: list[str]) -> list[dict]:
    members = list_members(group_id)
    lowered = {n.lower().lstrip("@") for n in names}
    matched = []
    for m in members:
        if m["runtime_status"] == "offline":
            continue
        if m["display_name"].lower() in lowered:
            matched.append(m)
            continue
        tpl = get_template(m["template_id"])
        if tpl and tpl["name"].lower() in lowered:
            matched.append(m)
    return matched


# ── Feed ──────────────────────────────────────────────────


def list_feed(group_id: str, limit: int = 200) -> list[dict]:
    page = list_feed_page(group_id, limit=limit)
    return page["items"]


def list_feed_page(
    group_id: str,
    *,
    limit: int = 10,
    before_ts: float | None = None,
) -> dict:
    """Return up to `limit` messages ending just before before_ts (or latest if None).

    Items are chronological (oldest → newest within the page).
    """
    limit = max(1, min(int(limit), 100))
    conn = get_connection()
    try:
        if before_ts is None:
            rows = conn.execute(
                """SELECT * FROM (
                     SELECT * FROM feed_events
                     WHERE group_id = ? AND visibility IN ('L1', 'L2')
                     ORDER BY ts DESC LIMIT ?
                   ) sub ORDER BY ts ASC""",
                (group_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT * FROM (
                     SELECT * FROM feed_events
                     WHERE group_id = ? AND visibility IN ('L1', 'L2') AND ts < ?
                     ORDER BY ts DESC LIMIT ?
                   ) sub ORDER BY ts ASC""",
                (group_id, before_ts, limit),
            ).fetchall()
        items = []
        for r in rows:
            d = row_to_dict(r)
            d["meta"] = _loads(d.get("meta"), {})
            items.append(d)
        has_more = False
        if items:
            oldest_ts = items[0]["ts"]
            older = conn.execute(
                """SELECT 1 FROM feed_events
                   WHERE group_id = ? AND visibility IN ('L1', 'L2') AND ts < ?
                   LIMIT 1""",
                (group_id, oldest_ts),
            ).fetchone()
            has_more = bool(older)
        elif before_ts is not None:
            has_more = False
        else:
            # empty latest page
            has_more = False
        return {"items": items, "has_more": has_more, "limit": limit}
    finally:
        conn.close()


def add_feed_event(
    group_id: str,
    *,
    speaker_type: str,
    speaker_id: str,
    content: str,
    visibility: str = "L2",
    meta: dict | None = None,
) -> dict:
    eid = _id("feed")
    ts = _now()
    meta = meta or {}
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        run_ids = [r["run_id"] for r in meta.get("runs", []) if r.get("run_id")]
        if meta.get("run_id"):
            run_ids.append(meta["run_id"])
        from server.runtime.run_fence import validate
        for rid in dict.fromkeys(run_ids):
            run = conn.execute("SELECT session_id FROM runtime_runs WHERE run_id=?", (rid,)).fetchone()
            if run:
                validate(conn, rid, run[0])
        conn.execute(
            """INSERT INTO feed_events
               (event_id, group_id, speaker_type, speaker_id, visibility, content, ts, meta)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (eid, group_id, speaker_type, speaker_id, visibility, content, ts, _json(meta)),
        )
        if not meta.get("ask_user") and not meta.get("ask_user_answer"):
            from server.runtime.session_history import record

            run_ids = [r["run_id"] for r in meta.get("runs", []) if r.get("run_id")]
            if meta.get("run_id"):
                run_ids.append(meta["run_id"])
            for rid in dict.fromkeys(run_ids):
                run = conn.execute("SELECT session_id,agent_id FROM runtime_runs WHERE run_id=?", (rid,)).fetchone()
                if run:
                    record(conn, run[0], "feed", eid, run_id=rid, instance_id=run[1], expected_epoch=meta.get("session_epoch"))
        conn.commit()
    finally:
        conn.close()
    return {
        "event_id": eid,
        "group_id": group_id,
        "speaker_type": speaker_type,
        "speaker_id": speaker_id,
        "visibility": visibility,
        "content": content,
        "ts": ts,
        "meta": meta,
    }


def list_l1_feed(group_id: str, limit: int = 30) -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT * FROM feed_events
               WHERE group_id = ? AND visibility = 'L1'
               ORDER BY ts DESC LIMIT ?""",
            (group_id, limit),
        ).fetchall()
        out = []
        for r in rows:
            d = row_to_dict(r)
            d["meta"] = _loads(d.get("meta"), {})
            out.append(d)
        return list(reversed(out))
    finally:
        conn.close()


# ── Tasks ─────────────────────────────────────────────────


def list_tasks(group_id: str) -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT * FROM group_tasks WHERE group_id = ?
               ORDER BY created_at ASC""",
            (group_id,),
        ).fetchall()
        out = []
        for r in rows:
            d = row_to_dict(r)
            d["blocked_by"] = _loads(d.get("blocked_by"), [])
            out.append(d)
        return out
    finally:
        conn.close()


def task_stats(group_id: str) -> dict:
    tasks = list_tasks(group_id)
    stats = {"pending": 0, "in_progress": 0, "completed": 0, "failed": 0, "cancelled": 0}
    for t in tasks:
        st = t.get("status", "pending")
        if st in stats:
            stats[st] += 1
    return stats


def create_task(
    group_id: str,
    title: str,
    *,
    description: str = "",
    owner_instance_id: str | None = None,
    blocked_by: list | None = None,
) -> dict:
    tid = _id("gtask")
    now = _now()
    blocked_by = blocked_by or []
    conn = get_connection()
    try:
        conn.execute(
            """INSERT INTO group_tasks
               (task_id, group_id, title, description, owner_instance_id, status, blocked_by, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?)""",
            (tid, group_id, title, description, owner_instance_id, _json(blocked_by), now, now),
        )
        conn.commit()
    finally:
        conn.close()
    return {
        "task_id": tid,
        "group_id": group_id,
        "title": title,
        "description": description,
        "owner_instance_id": owner_instance_id,
        "status": "pending",
        "blocked_by": blocked_by,
        "created_at": now,
        "updated_at": now,
    }


# ── App settings (key-value) ───────────────────────────────


def get_setting(key: str, default: str = "") -> str:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT value FROM app_settings WHERE key = ?", (key,)
        ).fetchone()
        if not row:
            return default
        return str(row["value"] or default)
    finally:
        conn.close()


def set_setting(key: str, value: str) -> None:
    conn = get_connection()
    try:
        conn.execute(
            """INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
            (key, value, _now()),
        )
        conn.commit()
    finally:
        conn.close()


def delete_setting(key: str) -> None:
    conn = get_connection()
    try:
        conn.execute("DELETE FROM app_settings WHERE key = ?", (key,))
        conn.commit()
    finally:
        conn.close()


# ── Runtime runs / goal bindings ───────────────────────────


def create_runtime_run(
    run_id: str,
    session_id: str,
    *,
    agent_id: str = "",
    status: str = "queued",
) -> dict:
    now = _now()
    conn = get_connection()
    try:
        from server.runtime.run_fence import bind
        bind(conn, run_id, session_id)
        conn.execute(
            """INSERT INTO runtime_runs
               (run_id, session_id, agent_id, status, cancel_requested, error, created_at, updated_at)
               VALUES (?, ?, ?, ?, 0, '', ?, ?)""",
            (run_id, session_id, agent_id, status, now, now),
        )
        conn.commit()
    finally:
        conn.close()
    return get_runtime_run(run_id) or {}


def upsert_runtime_run(
    run_id: str,
    session_id: str,
    *,
    agent_id: str = "",
    status: str,
) -> dict:
    now = _now()
    conn = get_connection()
    try:
        from server.runtime.run_fence import bind
        bind(conn, run_id, session_id)
        conn.execute(
            """INSERT INTO runtime_runs
               (run_id, session_id, agent_id, status, cancel_requested, error, created_at, updated_at)
               VALUES (?, ?, ?, ?, 0, '', ?, ?)
               ON CONFLICT(run_id) DO UPDATE SET
                 status=excluded.status,
                 agent_id=CASE WHEN excluded.agent_id != '' THEN excluded.agent_id ELSE runtime_runs.agent_id END,
                 updated_at=excluded.updated_at""",
            (run_id, session_id, agent_id, status, now, now),
        )
        conn.commit()
    finally:
        conn.close()
    return get_runtime_run(run_id) or {}


def get_runtime_run(run_id: str) -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM runtime_runs WHERE run_id = ?", (run_id,)).fetchone()
        return row_to_dict(row) if row else None
    finally:
        conn.close()


def finish_runtime_run(run_id: str, *, status: str, error: str = "") -> None:
    conn = get_connection()
    try:
        conn.execute(
            """UPDATE runtime_runs SET status = ?, error = ?, updated_at = ?
               WHERE run_id = ?""",
            (status, error[:1000], _now(), run_id),
        )
        conn.commit()
    finally:
        conn.close()


# ── LLM usage ──────────────────────────────────────────────────────────────


def add_llm_usage_event(event: dict[str, Any]) -> bool:
    """Insert one metered LLM call. A duplicate call_id is intentionally ignored."""
    conn = get_connection()
    try:
        cur = conn.execute(
            """INSERT OR IGNORE INTO llm_usage_events (
                event_id, call_id, run_id, session_id, agent_id, call_kind,
                provider, model, status, usage_source, cache_usage_source,
                input_tokens, cached_input_tokens, uncached_input_tokens,
                output_tokens, total_tokens, reasoning_tokens, cache_creation_tokens,
                started_at, completed_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event["event_id"], event["call_id"], event.get("run_id", ""),
                event.get("session_id", ""), event.get("agent_id", ""), event["call_kind"],
                event.get("provider", ""), event.get("model", ""), event["status"],
                event["usage_source"], event["cache_usage_source"],
                event["input_tokens"], event["cached_input_tokens"], event["uncached_input_tokens"],
                event["output_tokens"], event["total_tokens"], event.get("reasoning_tokens"),
                event.get("cache_creation_tokens"), event["started_at"], event["completed_at"],
                event["created_at"],
            ),
        )
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def get_run_usage_summary(run_id: str) -> dict | None:
    return get_usage_summary_for_runs([run_id])


def get_usage_summary_for_runs(run_ids: list[str]) -> dict | None:
    normalized_run_ids = list(
        dict.fromkeys(str(run_id).strip() for run_id in run_ids if str(run_id).strip())
    )
    if not normalized_run_ids:
        return None
    conn = get_connection()
    try:
        placeholders = ", ".join("?" for _ in normalized_run_ids)
        row = conn.execute(
            f"""SELECT
                COALESCE(SUM(input_tokens), 0) AS input_tokens,
                COALESCE(SUM(cached_input_tokens), 0) AS cached_input_tokens,
                COALESCE(SUM(uncached_input_tokens), 0) AS uncached_input_tokens,
                COALESCE(SUM(output_tokens), 0) AS output_tokens,
                COALESCE(SUM(total_tokens), 0) AS total_tokens,
                COUNT(*) AS call_count,
                SUM(CASE WHEN usage_source = 'provider' THEN 1 ELSE 0 END) AS provider_call_count,
                SUM(CASE WHEN usage_source = 'estimated' THEN 1 ELSE 0 END) AS estimated_call_count
            FROM llm_usage_events WHERE run_id IN ({placeholders})""",
            normalized_run_ids,
        ).fetchone()
        if not row or not row["call_count"]:
            return None
        summary = row_to_dict(row)
        summary["usage_source"] = (
            "provider" if summary["estimated_call_count"] == 0 else
            "estimated" if summary["provider_call_count"] == 0 else "mixed"
        )
        return summary
    finally:
        conn.close()


def _usage_filters(start_date: str, end_date: str, model: str = "", agent_id: str = "") -> tuple[str, list[str]]:
    clauses = ["date(completed_at, 'unixepoch', '+8 hours') BETWEEN ? AND ?"]
    params = [start_date, end_date]
    if model:
        clauses.append("model = ?")
        params.append(model)
    if agent_id:
        clauses.append("agent_id = ?")
        params.append(agent_id)
    return " AND ".join(clauses), params


def usage_overview(start_date: str, end_date: str, *, model: str = "", agent_id: str = "") -> dict:
    where, params = _usage_filters(start_date, end_date, model, agent_id)
    conn = get_connection()
    try:
        row = conn.execute(
            f"""SELECT COALESCE(SUM(input_tokens), 0) AS input_tokens,
                       COALESCE(SUM(cached_input_tokens), 0) AS cached_input_tokens,
                       COALESCE(SUM(uncached_input_tokens), 0) AS uncached_input_tokens,
                       COALESCE(SUM(output_tokens), 0) AS output_tokens,
                       COALESCE(SUM(total_tokens), 0) AS total_tokens,
                       COUNT(*) AS call_count,
                       SUM(CASE WHEN usage_source = 'provider' THEN 1 ELSE 0 END) AS provider_call_count,
                       SUM(CASE WHEN usage_source = 'estimated' THEN 1 ELSE 0 END) AS estimated_call_count,
                       SUM(CASE WHEN cache_usage_source = 'provider' THEN 1 ELSE 0 END) AS cache_available_call_count
                  FROM llm_usage_events WHERE {where}""",
            params,
        ).fetchone()
        return row_to_dict(row)
    finally:
        conn.close()


def usage_trend(start_date: str, end_date: str, *, model: str = "", agent_id: str = "") -> list[dict]:
    where, params = _usage_filters(start_date, end_date, model, agent_id)
    conn = get_connection()
    try:
        rows = conn.execute(
            f"""SELECT date(completed_at, 'unixepoch', '+8 hours') AS date, provider, model,
                       SUM(input_tokens) AS input_tokens, SUM(cached_input_tokens) AS cached_input_tokens,
                       SUM(uncached_input_tokens) AS uncached_input_tokens, SUM(output_tokens) AS output_tokens,
                       SUM(total_tokens) AS total_tokens, COUNT(*) AS call_count
                  FROM llm_usage_events WHERE {where}
                 GROUP BY date, provider, model ORDER BY date, model""",
            params,
        ).fetchall()
        return [row_to_dict(row) for row in rows]
    finally:
        conn.close()


def usage_by_model(start_date: str, end_date: str, *, model: str = "", agent_id: str = "") -> list[dict]:
    where, params = _usage_filters(start_date, end_date, model, agent_id)
    conn = get_connection()
    try:
        rows = conn.execute(
            f"""SELECT provider, model, SUM(input_tokens) AS input_tokens,
                       SUM(cached_input_tokens) AS cached_input_tokens,
                       SUM(uncached_input_tokens) AS uncached_input_tokens, SUM(output_tokens) AS output_tokens,
                       SUM(total_tokens) AS total_tokens, COUNT(*) AS call_count,
                       SUM(CASE WHEN usage_source = 'estimated' THEN 1 ELSE 0 END) AS estimated_call_count
                  FROM llm_usage_events WHERE {where}
                 GROUP BY provider, model ORDER BY total_tokens DESC, model""",
            params,
        ).fetchall()
        return [row_to_dict(row) for row in rows]
    finally:
        conn.close()


def usage_by_agent(start_date: str, end_date: str, *, model: str = "", agent_id: str = "") -> list[dict]:
    where, params = _usage_filters(start_date, end_date, model, agent_id)
    conn = get_connection()
    try:
        rows = conn.execute(
            f"""SELECT e.agent_id, COALESCE(t.name, '平台内部调用') AS agent_name,
                       SUM(e.input_tokens) AS input_tokens,
                       SUM(e.cached_input_tokens) AS cached_input_tokens,
                       SUM(e.uncached_input_tokens) AS uncached_input_tokens,
                       SUM(e.output_tokens) AS output_tokens,
                       SUM(e.total_tokens) AS total_tokens, COUNT(*) AS call_count,
                       SUM(CASE WHEN e.usage_source = 'estimated' THEN 1 ELSE 0 END) AS estimated_call_count
                  FROM llm_usage_events e LEFT JOIN agent_templates t ON t.template_id = e.agent_id
                 WHERE {where.replace('model', 'e.model').replace('agent_id', 'e.agent_id').replace('completed_at', 'e.completed_at')}
                 GROUP BY e.agent_id, t.name ORDER BY total_tokens DESC, agent_name""",
            params,
        ).fetchall()
        return [row_to_dict(row) for row in rows]
    finally:
        conn.close()


def usage_by_agent_model(start_date: str, end_date: str, *, model: str = "", agent_id: str = "") -> list[dict]:
    where, params = _usage_filters(start_date, end_date, model, agent_id)
    conn = get_connection()
    try:
        rows = conn.execute(
            f"""SELECT e.agent_id, e.model,
                       SUM(e.cached_input_tokens) AS cached_input_tokens,
                       SUM(e.uncached_input_tokens) AS uncached_input_tokens,
                       SUM(e.output_tokens) AS output_tokens
                  FROM llm_usage_events e
                 WHERE {where.replace('model', 'e.model').replace('agent_id', 'e.agent_id').replace('completed_at', 'e.completed_at')}
                 GROUP BY e.agent_id, e.model""",
            params,
        ).fetchall()
        return [row_to_dict(row) for row in rows]
    finally:
        conn.close()


def mark_runtime_run_cancel_requested(run_id: str) -> None:
    conn = get_connection()
    try:
        conn.execute(
            """UPDATE runtime_runs
               SET status = 'cancel_requested', cancel_requested = 1, updated_at = ?
               WHERE run_id = ?""",
            (_now(), run_id),
        )
        conn.commit()
    finally:
        conn.close()


def interrupt_stale_runtime_runs() -> int:
    conn = get_connection()
    try:
        cur = conn.execute(
            """UPDATE runtime_runs SET status = 'interrupted', updated_at = ?
               WHERE status IN ('queued', 'running', 'cancel_requested')""",
            (_now(),),
        )
        conn.commit()
        return int(cur.rowcount or 0)
    finally:
        conn.close()


def list_goal_task_ids(goal_id: str) -> list[str]:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT task_id FROM runtime_goal_tasks WHERE goal_id = ? ORDER BY bound_at",
            (goal_id,),
        ).fetchall()
        return [str(row[0]) for row in rows]
    finally:
        conn.close()


# ── Pending user questions ─────────────────────────────────


def _hydrate_pending_question(row) -> dict:
    d = row_to_dict(row)
    payload = _loads(d.get("payload"), {})
    if not isinstance(payload, dict):
        payload = {}
    d["payload"] = payload
    # Flatten for clients (Swift PendingQuestion.questions).
    d["questions"] = payload.get("questions") or []
    d["answer"] = _loads(d.get("answer"), None)
    d["session"] = d.get("session_id") or ""
    d["template"] = d.get("template_id") or ""
    d["group"] = d.get("group_id") or ""
    d["instance"] = d.get("instance_id") or ""
    return d


def create_pending_question(
    *,
    tool_call_id: str,
    goal_id: str,
    run_id: str,
    session_id: str,
    channel: str,
    surface: str,
    template_id: str,
    group_id: str,
    instance_id: str,
    payload: dict,
) -> dict:
    question_id = _id("pq")
    now = _now()
    conn = get_connection()
    try:
        from server.runtime.run_fence import validate
        validate(conn, run_id, session_id)
        conn.execute(
            """INSERT INTO pending_questions
               (question_id, tool_call_id, goal_id, run_id, session_id, channel,
                surface, template_id, group_id, instance_id, payload, status,
                answer, created_at, updated_at, answered_at, cancelled_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', NULL, ?, ?, NULL, NULL)""",
            (
                question_id,
                tool_call_id,
                goal_id,
                run_id,
                session_id,
                channel,
                surface,
                template_id,
                group_id,
                instance_id,
                _json(payload),
                now,
                now,
            ),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM pending_questions WHERE question_id = ?",
            (question_id,),
        ).fetchone()
        return _hydrate_pending_question(row)
    finally:
        conn.close()


def get_pending_question(question_id: str) -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM pending_questions WHERE question_id = ?",
            (question_id,),
        ).fetchone()
        return _hydrate_pending_question(row) if row else None
    finally:
        conn.close()


def list_pending_questions(channel: str) -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT * FROM pending_questions
               WHERE channel = ? AND status = 'pending'
               ORDER BY created_at ASC""",
            (channel,),
        ).fetchall()
        return [_hydrate_pending_question(row) for row in rows]
    finally:
        conn.close()


def answer_pending_question(question_id: str, answers: list[dict]) -> dict | None:
    now = _now()
    conn = get_connection()
    try:
        cur = conn.execute(
            """UPDATE pending_questions
               SET status = 'answered', answer = ?, answered_at = ?, updated_at = ?
               WHERE question_id = ? AND status = 'pending'""",
            (_json({"answers": answers}), now, now, question_id),
        )
        if cur.rowcount != 1:
            conn.rollback()
            return None
        row = conn.execute(
            "SELECT * FROM pending_questions WHERE question_id = ?",
            (question_id,),
        ).fetchone()
        from server.runtime.session_history import record
        from server.runtime.run_fence import validate

        validate(conn, row["run_id"], row["session_id"])
        record(conn, row["session_id"], "question", question_id, run_id=row["run_id"], instance_id=row["instance_id"])
        conn.commit()
        return _hydrate_pending_question(row)
    finally:
        conn.close()


def cancel_pending_question(question_id: str) -> dict | None:
    now = _now()
    conn = get_connection()
    try:
        cur = conn.execute(
            """UPDATE pending_questions
               SET status = 'cancelled', cancelled_at = ?, updated_at = ?
               WHERE question_id = ? AND status = 'pending'""",
            (now, now, question_id),
        )
        conn.commit()
        if cur.rowcount != 1:
            return None
        row = conn.execute(
            "SELECT * FROM pending_questions WHERE question_id = ?",
            (question_id,),
        ).fetchone()
        return _hydrate_pending_question(row)
    finally:
        conn.close()


def upsert_runtime_budget(
    root_budget_id: str,
    *,
    profile: str,
    limits: dict,
    consumed: dict,
    reserved: dict,
    active_execution_ms: int = 0,
    version: int = 1,
) -> None:
    """Persist BudgetState snapshot (authoritative across process restarts)."""
    now = _now()
    conn = get_connection()
    try:
        conn.execute(
            """INSERT INTO runtime_budgets
               (root_budget_id, profile, limits_json, consumed_json, reserved_json,
                active_execution_ms, version, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(root_budget_id) DO UPDATE SET
                 profile=excluded.profile,
                 limits_json=excluded.limits_json,
                 consumed_json=excluded.consumed_json,
                 reserved_json=excluded.reserved_json,
                 active_execution_ms=excluded.active_execution_ms,
                 version=excluded.version,
                 updated_at=excluded.updated_at""",
            (
                root_budget_id,
                profile,
                json.dumps(limits, ensure_ascii=False),
                json.dumps(consumed, ensure_ascii=False),
                json.dumps(reserved, ensure_ascii=False),
                int(active_execution_ms),
                int(version),
                now,
                now,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def get_runtime_budget(root_budget_id: str) -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM runtime_budgets WHERE root_budget_id = ?",
            (root_budget_id,),
        ).fetchone()
        if not row:
            return None
        data = dict(row)
        for key, field in (
            ("limits", "limits_json"),
            ("consumed", "consumed_json"),
            ("reserved", "reserved_json"),
        ):
            raw = data.get(field) or "{}"
            try:
                data[key] = json.loads(raw)
            except Exception:
                data[key] = {}
        return data
    finally:
        conn.close()


def upsert_budget_reservation(
    reservation_id: str,
    *,
    root_budget_id: str,
    kind: str,
    amount: float,
    status: str = "reserved",
) -> None:
    now = _now()
    conn = get_connection()
    try:
        conn.execute(
            """INSERT INTO budget_reservations
               (reservation_id, root_budget_id, kind, amount, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(reservation_id) DO UPDATE SET
                 status=excluded.status,
                 amount=excluded.amount,
                 updated_at=excluded.updated_at""",
            (reservation_id, root_budget_id, kind, float(amount), status, now, now),
        )
        conn.commit()
    finally:
        conn.close()


def delete_budget_reservation(reservation_id: str) -> None:
    conn = get_connection()
    try:
        conn.execute(
            "DELETE FROM budget_reservations WHERE reservation_id = ?",
            (reservation_id,),
        )
        conn.commit()
    finally:
        conn.close()


# ── IM channels ────────────────────────────────────────────


def _im_bot_row(row) -> dict | None:
    if not row:
        return None
    data = row_to_dict(row)
    data.pop("secret_ref", None)
    return data


def upsert_im_application(
    *, platform: str, provider_application_id: str, display_name: str = "", status: str = "connected"
) -> dict:
    now = _now()
    conn = get_connection()
    try:
        row = conn.execute(
            """SELECT * FROM im_channel_applications
               WHERE platform = ? AND provider_application_id = ?""",
            (platform, provider_application_id),
        ).fetchone()
        if row:
            application_id = str(row["application_id"])
            conn.execute(
                """UPDATE im_channel_applications
                   SET display_name = ?, status = ?, updated_at = ?
                   WHERE application_id = ?""",
                (display_name or row["display_name"], status, now, application_id),
            )
        else:
            application_id = _id("imapp")
            conn.execute(
                """INSERT INTO im_channel_applications
                   (application_id, platform, provider_application_id, display_name, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (application_id, platform, provider_application_id, display_name, status, now, now),
            )
        conn.commit()
        out = conn.execute(
            "SELECT * FROM im_channel_applications WHERE application_id = ?", (application_id,)
        ).fetchone()
        return row_to_dict(out)
    finally:
        conn.close()


def upsert_im_bot(
    *,
    application_id: str,
    provider_bot_id: str,
    display_name: str = "",
    secret_ref: str = "",
    health: str = "stopped",
) -> dict:
    now = _now()
    conn = get_connection()
    try:
        row = conn.execute(
            """SELECT bot_id, secret_ref FROM im_bots
               WHERE application_id = ? AND provider_bot_id = ?""",
            (application_id, provider_bot_id),
        ).fetchone()
        if row:
            bot_id = str(row["bot_id"])
            conn.execute(
                """UPDATE im_bots SET display_name = ?, secret_ref = ?, health = ?,
                   last_error = '', updated_at = ? WHERE bot_id = ?""",
                (display_name, secret_ref or row["secret_ref"], health, now, bot_id),
            )
        else:
            bot_id = _id("imbot")
            conn.execute(
                """INSERT INTO im_bots
                   (bot_id, application_id, provider_bot_id, display_name, secret_ref, health, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (bot_id, application_id, provider_bot_id, display_name, secret_ref, health, now, now),
            )
        conn.commit()
        return _im_bot_row(conn.execute("SELECT * FROM im_bots WHERE bot_id = ?", (bot_id,)).fetchone()) or {}
    finally:
        conn.close()


def get_im_bot(bot_id: str, *, include_secret_ref: bool = False) -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute(
            """SELECT b.*, a.platform, a.provider_application_id, a.display_name AS application_name,
                      t.name AS template_name
               FROM im_bots b
               JOIN im_channel_applications a ON a.application_id = b.application_id
               LEFT JOIN agent_templates t ON t.template_id = b.template_id
               WHERE b.bot_id = ?""",
            (bot_id,),
        ).fetchone()
        data = row_to_dict(row) if row else None
        if data and not include_secret_ref:
            data.pop("secret_ref", None)
        return data
    finally:
        conn.close()


def get_im_bot_by_provider(platform: str, provider_bot_id: str, *, include_secret_ref: bool = False) -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute(
            """SELECT b.*, a.platform, a.provider_application_id, a.display_name AS application_name,
                      t.name AS template_name
               FROM im_bots b
               JOIN im_channel_applications a ON a.application_id = b.application_id
               LEFT JOIN agent_templates t ON t.template_id = b.template_id
               WHERE a.platform = ? AND b.provider_bot_id = ?""",
            (platform, provider_bot_id),
        ).fetchone()
        data = row_to_dict(row) if row else None
        if data and not include_secret_ref:
            data.pop("secret_ref", None)
        return data
    finally:
        conn.close()


def list_im_channels(platform: str | None = None) -> list[dict]:
    conn = get_connection()
    try:
        where, args = ("", []) if not platform else ("WHERE a.platform = ?", [platform])
        apps = [row_to_dict(r) for r in conn.execute(
            f"SELECT a.* FROM im_channel_applications a {where} ORDER BY a.updated_at DESC", args
        ).fetchall()]
        for app in apps:
            rows = conn.execute(
                """SELECT b.bot_id, b.template_id, b.provider_bot_id, b.display_name, b.health,
                          b.last_error, b.created_at, b.updated_at, t.name AS template_name
                   FROM im_bots b LEFT JOIN agent_templates t ON t.template_id = b.template_id
                   WHERE b.application_id = ? ORDER BY b.created_at ASC""",
                (app["application_id"],),
            ).fetchall()
            app["bots"] = [row_to_dict(row) for row in rows]
        return apps
    finally:
        conn.close()


def list_im_bots_for_assistant(template_id: str) -> dict:
    conn = get_connection()
    try:
        bound = conn.execute(
            """SELECT b.bot_id, b.template_id, b.provider_bot_id, b.display_name, b.health, b.last_error,
                      a.platform, a.provider_application_id, a.display_name AS application_name
               FROM im_bots b JOIN im_channel_applications a ON a.application_id = b.application_id
               WHERE b.template_id = ? ORDER BY a.platform, b.created_at""",
            (template_id,),
        ).fetchall()
        available = conn.execute(
            """SELECT b.bot_id, b.template_id, b.provider_bot_id, b.display_name, b.health, b.last_error,
                      a.platform, a.provider_application_id, a.display_name AS application_name
               FROM im_bots b JOIN im_channel_applications a ON a.application_id = b.application_id
               WHERE b.template_id IS NULL ORDER BY a.platform, b.created_at"""
        ).fetchall()
        return {"bound": [row_to_dict(r) for r in bound], "available": [row_to_dict(r) for r in available]}
    finally:
        conn.close()


def bind_im_bot(bot_id: str, template_id: str | None) -> dict | None:
    now = _now()
    conn = get_connection()
    try:
        if template_id:
            exists = conn.execute("SELECT 1 FROM agent_templates WHERE template_id = ?", (template_id,)).fetchone()
            if not exists:
                return None
        row = conn.execute("SELECT bot_id FROM im_bots WHERE bot_id = ?", (bot_id,)).fetchone()
        if not row:
            return None
        conn.execute("UPDATE im_bots SET template_id = ?, updated_at = ? WHERE bot_id = ?", (template_id, now, bot_id))
        conn.commit()
        return get_im_bot(bot_id)
    finally:
        conn.close()


def set_im_bot_health(bot_id: str, health: str, last_error: str = "") -> None:
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE im_bots SET health = ?, last_error = ?, updated_at = ? WHERE bot_id = ?",
            (health, last_error[:500], _now(), bot_id),
        )
        conn.commit()
    finally:
        conn.close()


def delete_im_bot(bot_id: str) -> dict | None:
    bot = get_im_bot(bot_id, include_secret_ref=True)
    if not bot:
        return None
    conn = get_connection()
    try:
        conn.execute("DELETE FROM im_bots WHERE bot_id = ?", (bot_id,))
        conn.commit()
        return bot
    finally:
        conn.close()


def get_im_conversation(bot_id: str, kind: str, remote_conversation_id: str) -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute(
            """SELECT c.*, b.template_id, a.platform, b.provider_bot_id
               FROM im_conversations c
               JOIN im_bots b ON b.bot_id = c.bot_id
               JOIN im_channel_applications a ON a.application_id = b.application_id
               WHERE c.bot_id = ? AND c.kind = ? AND c.remote_conversation_id = ?""",
            (bot_id, kind, remote_conversation_id),
        ).fetchone()
        return row_to_dict(row) if row else None
    finally:
        conn.close()


def create_im_conversation(
    *, bot_id: str, kind: str, remote_conversation_id: str, session_id: str, display_name: str = ""
) -> dict:
    now = _now()
    conn = get_connection()
    try:
        existing = conn.execute(
            """SELECT * FROM im_conversations
               WHERE bot_id = ? AND kind = ? AND remote_conversation_id = ?""",
            (bot_id, kind, remote_conversation_id),
        ).fetchone()
        if existing:
            return row_to_dict(existing)
        conversation_id = _id("imconv")
        conn.execute(
            """INSERT INTO im_conversations
               (conversation_id, bot_id, kind, remote_conversation_id, session_id, display_name, last_inbound_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (conversation_id, bot_id, kind, remote_conversation_id, session_id, display_name, now),
        )
        conn.commit()
        return row_to_dict(conn.execute("SELECT * FROM im_conversations WHERE conversation_id = ?", (conversation_id,)).fetchone())
    finally:
        conn.close()


def touch_im_conversation(conversation_id: str) -> None:
    conn = get_connection()
    try:
        conn.execute("UPDATE im_conversations SET last_inbound_at = ? WHERE conversation_id = ?", (_now(), conversation_id))
        conn.commit()
    finally:
        conn.close()


def im_conversation_for_session(session_id: str) -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute(
            """SELECT c.*, a.platform, b.provider_bot_id, b.display_name AS bot_name
               FROM im_conversations c
               JOIN im_bots b ON b.bot_id = c.bot_id
               JOIN im_channel_applications a ON a.application_id = b.application_id
               WHERE c.session_id = ?""",
            (session_id,),
        ).fetchone()
        return row_to_dict(row) if row else None
    finally:
        conn.close()


def claim_im_message(*, platform: str, bot_id: str, provider_message_id: str, conversation_id: str) -> bool:
    if not provider_message_id:
        return True
    conn = get_connection()
    try:
        try:
            conn.execute(
                """INSERT INTO im_message_receipts
                   (platform, bot_id, provider_message_id, conversation_id, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (platform, bot_id, provider_message_id, conversation_id, _now()),
            )
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            return False
    finally:
        conn.close()


def mark_session_unread(session_id: str) -> None:
    now = _now()
    conn = get_connection()
    try:
        conn.execute(
            """INSERT INTO session_unreads (session_id, unread_count, last_unread_at)
               VALUES (?, 1, ?)
               ON CONFLICT(session_id) DO UPDATE SET
                 unread_count = session_unreads.unread_count + 1,
                 last_unread_at = excluded.last_unread_at""",
            (session_id, now),
        )
        conn.commit()
    finally:
        conn.close()


def clear_session_unread(session_id: str) -> None:
    conn = get_connection()
    try:
        conn.execute("DELETE FROM session_unreads WHERE session_id = ?", (session_id,))
        conn.commit()
    finally:
        conn.close()


def session_unread(session_id: str) -> bool:
    return session_unread_count(session_id) > 0


def session_unread_count(session_id: str) -> int:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT unread_count FROM session_unreads WHERE session_id = ?", (session_id,)
        ).fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


def assistant_has_unread(template_id: str) -> bool:
    return assistant_unread_count(template_id) > 0


def assistant_unread_count(template_id: str) -> int:
    conn = get_connection()
    try:
        row = conn.execute(
            """SELECT COALESCE(SUM(u.unread_count), 0)
               FROM session_unreads u
               JOIN im_conversations c ON c.session_id = u.session_id
               JOIN im_bots b ON b.bot_id = c.bot_id
               WHERE b.template_id = ? AND u.unread_count > 0""",
            (template_id,),
        ).fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()
