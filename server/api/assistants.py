from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from server.db import repos
from server.scheduler import group_scheduler as scheduler

router = APIRouter(prefix="/api/assistants", tags=["assistants"])

AVATAR_IDS = frozenset(
    f"{kind}-avatars-{index:02d}"
    for kind in ("animal", "human", "plant")
    for index in range(1, 10)
)


def validate_avatar_id(avatar_id: str) -> str:
    value = avatar_id.strip()
    if value and value not in AVATAR_IDS:
        raise HTTPException(400, "invalid avatar_id")
    return value


class TemplateCreate(BaseModel):
    name: str
    role: str = ""
    system_prompt: str = ""
    tools_allowlist: list[str] = Field(default_factory=list)
    theme_color: str = "#5db8a6"
    avatar_id: str = ""
    model_profile_id: str = ""


class TemplateUpdate(BaseModel):
    name: str | None = None
    role: str | None = None
    system_prompt: str | None = None
    theme_color: str | None = None
    avatar_id: str | None = None
    model_profile_id: str | None = None
    web_enabled: bool | None = None
    file_access: str | None = None
    danger_policy: str | None = None
    danger_rules: list[str] | None = None
    # tools_allowlist deprecated — use PUT /api/assistants/{id}/capabilities


class AttachmentIn(BaseModel):
    path: str
    name: str = ""
    mime: str = "application/octet-stream"


class MessageCreate(BaseModel):
    content: str = ""
    aggregate_group_ids: list[str] = Field(default_factory=list)
    attachments: list[AttachmentIn] = Field(default_factory=list)
    session_id: str | None = None


class SessionRename(BaseModel):
    title: str


class MemoryConfirm(BaseModel):
    body: str
    source_groups: list[str] = Field(default_factory=list)
    message_id: str | None = None
    event_id: str | None = None
    group_id: str | None = None
    source: str | None = None
    type: str = "unknown"
    summary: str | None = None
    scope_kind: str = "assistant"
    scope_id: str | None = None
    idempotency_key: str | None = None


class MemoryDismiss(BaseModel):
    message_id: str | None = None
    event_id: str | None = None
    group_id: str | None = None


@router.get("")
def list_assistants():
    out = []
    for t in repos.list_templates():
        row = dict(t)
        row["last_message_preview"] = repos.latest_assistant_message_preview(t["template_id"])
        row["unread_count"] = repos.assistant_unread_count(t["template_id"])
        out.append(row)
    return out


@router.post("")
def create_assistant(body: TemplateCreate):
    from server.presets.default_capabilities import defaults_for_role
    from server.runtime.turn import default_model_profile_id

    caps = defaults_for_role(body.role, body.name)
    if body.tools_allowlist:
        caps["tools"] = list(body.tools_allowlist)
    return repos.create_template(
        name=body.name,
        role=body.role,
        system_prompt=body.system_prompt,
        tools_allowlist=caps.get("tools") or [],
        capabilities=caps,
        theme_color=body.theme_color,
        avatar_id=validate_avatar_id(body.avatar_id),
        model_profile_id=body.model_profile_id or default_model_profile_id(),
    )


@router.get("/{template_id}")
def get_assistant(template_id: str):
    tpl = repos.get_template(template_id)
    if not tpl:
        raise HTTPException(404, "assistant not found")
    row = dict(tpl)
    row["last_message_preview"] = repos.latest_assistant_message_preview(template_id)
    row["unread_count"] = repos.assistant_unread_count(template_id)
    return row


@router.patch("/{template_id}")
def patch_assistant(template_id: str, body: TemplateUpdate):
    tpl = repos.get_template(template_id)
    if not tpl:
        raise HTTPException(404, "assistant not found")
    raw = body.model_dump(exclude_unset=True)
    if "avatar_id" in raw:
        raw["avatar_id"] = validate_avatar_id(raw["avatar_id"])
    if "tools_allowlist" in raw:
        raise HTTPException(
            400,
            "tools_allowlist is deprecated; use PUT /api/assistants/{id}/capabilities",
        )
    meta = dict(tpl.get("config_meta") or {})
    meta_changed = False
    web_enabled = raw.pop("web_enabled", None)
    if web_enabled is not None:
        meta["web_enabled"] = bool(web_enabled)
        meta_changed = True
    from server.runtime.policy import policy_to_meta_patch

    try:
        patch = policy_to_meta_patch(
            file_access=raw.pop("file_access", None),
            danger_policy=raw.pop("danger_policy", None),
            danger_rules=raw.pop("danger_rules", None),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if patch:
        meta.update(patch)
        meta_changed = True
    if meta_changed:
        raw["config_meta"] = meta
    updated = repos.update_template(template_id, **raw)
    if not updated:
        raise HTTPException(404, "assistant not found")
    row = dict(updated)
    row["last_message_preview"] = repos.latest_assistant_message_preview(template_id)
    return row


@router.delete("/{template_id}")
def delete_assistant(template_id: str):
    if not repos.get_template(template_id):
        raise HTTPException(404, "assistant not found")
    from server.db import mesh_repos

    if mesh_repos.has_active_tasks(template_id):
        raise HTTPException(409, "assistant has active collaboration tasks")
    if repos.template_in_use(template_id):
        raise HTTPException(400, "assistant still used in a project group")
    from server.api import assistant_session_ops as sess

    sess.purge_all_for_assistant(template_id)
    repos.delete_template(template_id)
    return {"ok": True}


@router.get("/{template_id}/sessions")
def list_sessions(template_id: str):
    if not repos.get_template(template_id):
        raise HTTPException(404, "assistant not found")
    from server.api import assistant_session_ops as sess

    return sess.list_sessions_response(template_id)


@router.post("/{template_id}/sessions")
def new_session(template_id: str):
    if not repos.get_template(template_id):
        raise HTTPException(404, "assistant not found")
    from server.api import assistant_session_ops as sess

    sess.migrate_assistant_sessions(template_id)
    return sess.new_session(template_id)


@router.post("/{template_id}/sessions/clear")
def clear_session(template_id: str, request: Request):
    if not repos.get_template(template_id):
        raise HTTPException(404, "assistant not found")
    from server.api import assistant_session_ops as sess
    from server.runtime.session_actors import HEADER_NAME

    actor = request.headers.get(HEADER_NAME) or request.headers.get("x-avent-actor")
    sess.migrate_assistant_sessions(template_id)
    out = sess.clear_current(template_id, actor=actor)
    if out.get("error") == "forbidden":
        raise HTTPException(403, out.get("detail") or "actor required")
    return out


@router.post("/{template_id}/sessions/{session_id}/activate")
def activate_session(template_id: str, session_id: str):
    if not repos.get_template(template_id):
        raise HTTPException(404, "assistant not found")
    from server.api import assistant_session_ops as sess

    sess.migrate_assistant_sessions(template_id)
    out = sess.activate_session(template_id, session_id)
    if out.get("error") == "not_found":
        raise HTTPException(404, "session not found")
    return out


@router.post("/{template_id}/sessions/{session_id}/read")
def mark_session_read(template_id: str, session_id: str):
    if not repos.get_template(template_id):
        raise HTTPException(404, "assistant not found")
    from server.api import assistant_session_ops as sess

    state = sess.get_sessions().load(session_id)
    if not state or state.meta.scope_key != sess.scope_for(template_id):
        raise HTTPException(404, "session not found")
    repos.clear_session_unread(session_id)
    return {"ok": True}


@router.patch("/{template_id}/sessions/{session_id}")
def rename_session(template_id: str, session_id: str, body: SessionRename):
    if not repos.get_template(template_id):
        raise HTTPException(404, "assistant not found")
    from server.api import assistant_session_ops as sess

    out = sess.rename_session(template_id, session_id, body.title)
    if not out:
        raise HTTPException(400, "invalid session or empty title")
    return out


@router.delete("/{template_id}/sessions/{session_id}")
def delete_session(template_id: str, session_id: str, request: Request):
    if not repos.get_template(template_id):
        raise HTTPException(404, "assistant not found")
    from server.api import assistant_session_ops as sess
    from server.runtime.session_actors import HEADER_NAME

    actor = request.headers.get(HEADER_NAME) or request.headers.get("x-avent-actor")
    out = sess.delete_session(template_id, session_id, actor=actor)
    if out.get("error") == "not_found":
        raise HTTPException(404, "session not found")
    if out.get("error") == "forbidden":
        raise HTTPException(403, out.get("detail") or "actor required")
    return out


@router.get("/{template_id}/messages")
def get_messages(
    template_id: str,
    session_id: str | None = None,
    limit: int = 10,
    before_ts: float | None = None,
):
    if not repos.get_template(template_id):
        raise HTTPException(404, "assistant not found")
    if not session_id:
        raise HTTPException(400, "session_id is required")
    return repos.list_assistant_messages_page(
        template_id, session_id=session_id, limit=limit, before_ts=before_ts
    )


@router.post("/{template_id}/messages")
def post_message(template_id: str, body: MessageCreate):
    if not repos.get_template(template_id):
        raise HTTPException(404, "assistant not found")
    from server.api import assistant_session_ops as sess
    from server.runtime.turn import format_message_with_attachments

    atts = [a.model_dump() for a in body.attachments]
    content = format_message_with_attachments(body.content, atts)
    if not content:
        raise HTTPException(400, "empty message")
    sess.migrate_assistant_sessions(template_id)
    try:
        sid = sess.resolve_session_for_post(
            template_id, body.session_id, title_hint=body.content or content
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    from server.runtime.goal import GoalController, parse_goal_command
    from server.runtime.run_coordinator import RunBusyError, coordinator

    command = parse_goal_command(content)
    if command and command.action in {"status", "clear"}:
        sm = sess.get_sessions()
        state = sm.load(sid)
        if state is None:
            raise HTTPException(404, "session not found")
        controller = GoalController(
            state=state,
            session_manager=sm,
            board_scope=f"assistant:{template_id}",
            run_id="",
            user_request="",
        )
        reply = controller.status_text()
        if command.action == "clear":
            coordinator.cancel_session(sid)
            state = sm.load(sid) or state
            controller.state = state
            controller.clear()
            reply = "Goal 已清除。"
        msg = repos.add_assistant_message(
            template_id,
            "system",
            reply,
            session_id=sid,
            meta={"goal_command": command.action, "runs": []},
        )
        sess.sync_after_message(template_id, sid)
        return {**msg, "runs": []}
    try:
        run_id = coordinator.enqueue(sid, agent_id=template_id)
    except RunBusyError as exc:
        raise HTTPException(409, str(exc)) from exc
    if command and command.action in {"set", "resume"}:
        sm = sess.get_sessions()
        state = sm.load(sid)
        if state is None:
            raise HTTPException(404, "session not found")
        controller = GoalController(
            state=state,
            session_manager=sm,
            board_scope=f"assistant:{template_id}",
            run_id=run_id,
            user_request=command.condition,
        )
        if command.action == "set":
            controller.activate(command.condition, source="explicit")
            content = command.condition
        else:
            resumed = controller.resume()
            if not resumed:
                coordinator.cancel(run_id)
                raise HTTPException(409, "no suspended goal to resume")
            content = str(resumed.get("condition") or "")
    meta = {"attachments": atts} if atts else {}
    if command:
        meta["goal_command"] = command.action
    meta["runs"] = [{"agent_id": template_id, "run_id": run_id}]
    # User-typed POST bodies must stay role=user so the DM UI does not render
    # them as assistant bubbles (legacy bug stored /goal as system).
    msg = repos.add_assistant_message(
        template_id,
        "user",
        body.content,
        session_id=sid,
        meta=meta,
    )
    sess.sync_after_message(template_id, sid)
    scheduler.run_assistant_turn_async(
        template_id,
        content,
        aggregate_group_ids=body.aggregate_group_ids,
        attachments=atts,
        session_id=sid,
        run_id=run_id,
    )
    return {**msg, "runs": meta["runs"]}


@router.get("/{template_id}/memories")
def get_memories(template_id: str, status: str = "active", type: str | None = None,
                 scope_kind: str | None = None, scope_id: str | None = None):
    if not repos.get_template(template_id):
        raise HTTPException(404, "assistant not found")
    return repos.list_assistant_memories(template_id, status=status, type=type, scope_kind=scope_kind, scope_id=scope_id)


@router.post("/{template_id}/memories/confirm")
def confirm_memory(template_id: str, body: MemoryConfirm):
    if not repos.get_template(template_id):
        raise HTTPException(404, "assistant not found")
    if not body.body.strip():
        raise HTTPException(400, "empty body")
    source = (body.source or "").strip().lower()
    if not source:
        source = "manual" if not body.message_id and not body.event_id else "confirm"
    from server.db import memory_store
    conn = repos.get_connection()
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            return memory_store.confirm(conn, template_id, body.body.strip(),
                message_id=body.message_id, event_id=body.event_id, group_id=body.group_id,
                idempotency_key=body.idempotency_key, source_groups=body.source_groups,
                source=source, type=body.type, summary=body.summary,
                scope_kind=body.scope_kind, scope_id=body.scope_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except memory_store.MemoryConflict as exc:
        raise HTTPException(409, {"message": str(exc), "current_version": exc.current_version}) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    finally:
        conn.close()


@router.post("/{template_id}/memories/dismiss")
def dismiss_memory(template_id: str, body: MemoryDismiss):
    if not repos.get_template(template_id):
        raise HTTPException(404, "assistant not found")
    from server.db import memory_store
    conn = repos.get_connection()
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            memory_store.dismiss(conn, template_id, message_id=body.message_id,
                                 event_id=body.event_id, group_id=body.group_id)
        return {"ok": True}
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except memory_store.MemoryConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    finally:
        conn.close()


class MemoryUpdate(BaseModel):
    body: str
    expected_version: int | None = None
    type: str | None = None
    summary: str | None = None


@router.patch("/{template_id}/memories/{memory_id}")
def patch_memory(template_id: str, memory_id: str, body: MemoryUpdate):
    if not repos.get_template(template_id):
        raise HTTPException(404, "assistant not found")
    if not body.body.strip():
        raise HTTPException(400, "empty body")
    from server.db.memory_store import MemoryConflict
    try:
        updated = repos.update_assistant_memory(template_id, memory_id, body.body.strip(),
            expected_version=body.expected_version, type=body.type, summary=body.summary)
    except MemoryConflict as exc:
        raise HTTPException(409, {"message": str(exc), "current_version": exc.current_version}) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not updated:
        raise HTTPException(404, "memory not found")
    return updated


@router.delete("/{template_id}/memories/{memory_id}")
def delete_memory(template_id: str, memory_id: str, expected_version: int | None = None):
    if not repos.get_template(template_id):
        raise HTTPException(404, "assistant not found")
    from server.db.memory_store import MemoryConflict
    try:
        deleted = repos.delete_assistant_memory(template_id, memory_id, expected_version=expected_version)
    except MemoryConflict as exc:
        raise HTTPException(409, {"message": str(exc), "current_version": exc.current_version}) from exc
    if not deleted:
        raise HTTPException(404, "memory not found")
    return {"ok": True}


@router.get("/{template_id}/memories/{memory_id}")
def get_memory(template_id: str, memory_id: str):
    memory = repos.get_assistant_memory(template_id, memory_id)
    if not memory:
        raise HTTPException(404, "memory not found")
    return memory


@router.get("/{template_id}/memories/{memory_id}/revisions")
def get_memory_revisions(template_id: str, memory_id: str):
    from server.db import memory_store
    conn = repos.get_connection()
    try:
        if not memory_store.get(conn, template_id, memory_id, include_deleted=True):
            raise HTTPException(404, "memory not found")
        return memory_store.revisions(conn, template_id, memory_id)
    finally:
        conn.close()


class MemoryRestore(BaseModel):
    revision_version: int
    expected_version: int


@router.post("/{template_id}/memories/{memory_id}/restore")
def restore_memory(template_id: str, memory_id: str, body: MemoryRestore):
    from server.db import memory_store
    conn = repos.get_connection()
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            result = memory_store.restore(conn, template_id, memory_id, body.revision_version, body.expected_version)
            if not result:
                raise HTTPException(404, "memory revision not found")
            return result
    except memory_store.MemoryConflict as exc:
        raise HTTPException(409, {"message": str(exc), "current_version": exc.current_version}) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    finally:
        conn.close()
