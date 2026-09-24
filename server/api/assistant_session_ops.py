"""Assistant DM session state machine helpers (see design 2026-07-13)."""

from __future__ import annotations

import logging
from typing import Any

from server.db import repos
from session_store import SessionManager

log = logging.getLogger("avent.assistant_sessions")

DEFAULT_TITLE = "新对话"
_DEFAULT_TITLES = frozenset({"", "New session", "新对话"})


def get_sessions() -> SessionManager:
    from server.runtime.turn import _get_sessions

    return _get_sessions()


def scope_for(template_id: str) -> str:
    from server.runtime.turn import scope_assistant

    return scope_assistant(template_id)


def migrate_assistant_sessions(template_id: str) -> None:
    sm = get_sessions()
    scope = scope_for(template_id)
    sid = sm.resolve_session_id(scope)
    missing = repos.count_messages_missing_session(template_id)
    if missing > 0 and not sid:
        state = sm.create_session("assistant_dm", scope, title=DEFAULT_TITLE)
        sid = state.meta.session_id
        log.info("migrate: created session %s for %s (%s orphan msgs)", sid, template_id, missing)
    if sid:
        n = repos.backfill_null_session_ids(template_id, sid)
        if n:
            log.info("migrate: backfilled %s msgs → %s", n, sid)
    for meta in sm.list_sessions_for_scope(scope, include_empty=True, include_archived=True):
        _sync_one(sm, template_id, meta.session_id)


def _sync_one(sm: SessionManager, template_id: str, session_id: str) -> None:
    from server.runtime.run_coordinator import coordinator
    from session_store import SessionVersionConflict

    # Session-list refreshes are read paths. Never let them advance the
    # canonical state version while a queued/running turn holds a snapshot.
    if coordinator.is_busy(session_id):
        return
    n = repos.count_assistant_messages(template_id, session_id)
    ts = repos.latest_message_ts(template_id, session_id)
    state = sm.load(session_id)
    title = None
    if state and state.meta.title in _DEFAULT_TITLES and n:
        first = repos.first_user_message_content(template_id, session_id)
        if first:
            from session_store import _title_from_hint

            title = _title_from_hint(first)
    try:
        sm.sync_meta_counts(
            session_id,
            message_count=n,
            last_active_at=ts,
            title=title,
        )
    except SessionVersionConflict:
        log.debug("skip meta sync for busy/raced session %s", session_id)


def _sqlite_count(template_id: str, session_id: str) -> int:
    return repos.count_assistant_messages(template_id, session_id)


def current_payload(template_id: str) -> dict[str, Any] | None:
    sm = get_sessions()
    scope = scope_for(template_id)
    sid = sm.resolve_session_id(scope)
    if not sid:
        return None
    state = sm.load(sid)
    if not state:
        return None
    n = _sqlite_count(template_id, sid)
    return {
        "session_id": sid,
        "title": state.meta.title or DEFAULT_TITLE,
        "message_count": n,
    }


def history_items(template_id: str) -> list[dict[str, Any]]:
    sm = get_sessions()
    scope = scope_for(template_id)
    items: list[dict[str, Any]] = []
    # SQLite is authoritative for DM messages. Session-state message_count only
    # tracks runtime context and can remain zero for IM-originated messages.
    for meta in sm.list_sessions_for_scope(scope, include_empty=True, include_archived=True):
        n = _sqlite_count(template_id, meta.session_id)
        if n <= 0:
            continue
        ts = repos.latest_message_ts(template_id, meta.session_id)
        item = {
            "session_id": meta.session_id,
            "title": meta.title or DEFAULT_TITLE,
            "last_message_at": ts or meta.last_active_at,
            "message_count": n,
            "unread_count": repos.session_unread_count(meta.session_id),
        }
        channel = repos.im_conversation_for_session(meta.session_id)
        if channel:
            item["im_channel"] = {
                "platform": channel["platform"],
                "kind": channel["kind"],
                "display_name": channel.get("display_name") or channel.get("bot_name") or "",
            }
        items.append(item)
    items.sort(key=lambda x: float(x.get("last_message_at") or 0), reverse=True)
    return items


def list_sessions_response(template_id: str) -> dict[str, Any]:
    migrate_assistant_sessions(template_id)
    return {"current": current_payload(template_id), "items": history_items(template_id)}


def _purge_if_cleared(template_id: str, sid: str) -> None:
    """If session has zero SQLite messages, physically delete it (anti-orphan)."""
    if _sqlite_count(template_id, sid) > 0:
        return
    sm = get_sessions()
    sm.purge_session(sid)
    repos.delete_assistant_messages_for_session(template_id, sid)


def leave_current_for_pending(template_id: str) -> None:
    """Archive-or-purge current, unbind → pending."""
    sm = get_sessions()
    scope = scope_for(template_id)
    sid = sm.resolve_session_id(scope)
    if not sid:
        return
    from server.runtime.run_coordinator import coordinator

    coordinator.cancel_session(sid)
    if _sqlite_count(template_id, sid) == 0:
        sm.purge_session(sid)
        repos.delete_assistant_messages_for_session(template_id, sid)
    else:
        sm.archive_session(sid)
        # archive_session already removes from scope_map
    sm.unbind_scope(scope)


def new_session(template_id: str) -> dict[str, Any]:
    leave_current_for_pending(template_id)
    return {"current": None}


def activate_session(template_id: str, session_id: str) -> dict[str, Any]:
    sm = get_sessions()
    scope = scope_for(template_id)
    state = sm.load(session_id)
    if not state or state.meta.scope_key != scope:
        return {"error": "not_found"}
    cur = sm.resolve_session_id(scope)
    if cur and cur != session_id:
        if _sqlite_count(template_id, cur) == 0:
            sm.purge_session(cur)
            repos.delete_assistant_messages_for_session(template_id, cur)
        else:
            old = sm.load(cur)
            if old:
                sm.patch_meta(cur, status="archived")
            sm.unbind_scope(scope)
    sm.bind_scope(scope, session_id)
    sm.patch_meta(session_id, status="active", last_active_at=__import__("time").time())
    _sync_one(sm, template_id, session_id)
    repos.clear_session_unread(session_id)
    page = repos.list_assistant_messages_page(template_id, session_id=session_id, limit=50)
    return {
        "current": current_payload(template_id),
        "messages": page,
    }


def rename_session(template_id: str, session_id: str, title: str) -> dict[str, Any] | None:
    sm = get_sessions()
    scope = scope_for(template_id)
    state = sm.load(session_id)
    if not state or state.meta.scope_key != scope:
        return None
    clean = (title or "").strip()
    if not clean:
        return None
    state = sm.patch_meta(session_id, title=clean[:120])
    return {
        "session_id": session_id,
        "title": state.meta.title,
        "message_count": _sqlite_count(template_id, session_id),
    }


def delete_session(
    template_id: str,
    session_id: str,
    *,
    actor: str | None = None,
) -> dict[str, Any]:
    from server.runtime.session_actors import assert_destructive_actor

    try:
        actor_norm = assert_destructive_actor(actor)
    except ValueError as exc:
        return {"error": "forbidden", "detail": str(exc)}
    sm = get_sessions()
    scope = scope_for(template_id)
    state = sm.load(session_id)
    if not state or state.meta.scope_key != scope:
        return {"error": "not_found"}
    from server.runtime.run_coordinator import coordinator
    from server.runtime.session_archive import archive_session_snapshot

    msgs = repos.export_assistant_messages_for_session(template_id, session_id)
    archive_session_snapshot(
        session_id=session_id,
        template_id=template_id,
        actor=actor_norm,
        reason="delete_session",
        sqlite_messages=msgs,
        state_payload={
            "meta": state.meta.__dict__ if hasattr(state.meta, "__dict__") else {},
            "messages": state.messages,
            "context": state.context,
            "todos": state.todos,
            "compact_summary": state.compact_summary,
            "version": state.version,
        },
    )
    coordinator.cancel_session(session_id)
    from server.runtime.session_history import invalidate

    invalidate(session_id, deleted=True)
    was_current = sm.resolve_session_id(scope) == session_id
    repos.delete_assistant_messages_for_session(template_id, session_id)
    sm.purge_session(session_id)
    if was_current:
        sm.unbind_scope(scope)
    return {"ok": True, "current": None if was_current else current_payload(template_id)}


def clear_current(
    template_id: str,
    *,
    actor: str | None = None,
    reason: str = "clear_current",
) -> dict[str, Any]:
    from server.runtime.session_actors import assert_destructive_actor

    try:
        actor_norm = assert_destructive_actor(actor)
    except ValueError as exc:
        return {"error": "forbidden", "detail": str(exc)}
    sm = get_sessions()
    scope = scope_for(template_id)
    sid = sm.resolve_session_id(scope)
    if not sid:
        return {"current": None}
    from server.runtime.run_coordinator import coordinator
    from server.runtime.session_archive import archive_session_snapshot

    state = sm.load(sid)
    msgs = repos.export_assistant_messages_for_session(template_id, sid)
    archive_session_snapshot(
        session_id=sid,
        template_id=template_id,
        actor=actor_norm,
        reason=reason,
        sqlite_messages=msgs,
        state_payload={
            "meta": state.meta.__dict__ if state and hasattr(state.meta, "__dict__") else {},
            "messages": list(state.messages) if state else [],
            "context": dict(state.context) if state else {},
            "todos": list(state.todos) if state else [],
            "compact_summary": state.compact_summary if state else "",
            "version": state.version if state else 0,
        },
    )
    # Cancel in-flight run, but history is already archived.
    coordinator.cancel_session(sid)
    from server.runtime.session_history import invalidate

    def clear_under_fence(conn, epoch):
        conn.execute("DELETE FROM assistant_messages WHERE template_id=? AND session_id=?", (template_id, sid))
        sm.clear_session_contents(sid, title=DEFAULT_TITLE, session_epoch=epoch, _fence_connection=conn)

    with sm._lock:
        invalidate(sid, on_invalidate=clear_under_fence)
    return {"current": current_payload(template_id)}


def resolve_session_for_post(
    template_id: str, session_id: str | None, *, title_hint: str = ""
) -> str:
    """Three-way POST contract → concrete session_id (creates if pending)."""
    sm = get_sessions()
    scope = scope_for(template_id)
    explicit = (session_id or "").strip()
    if explicit:
        state = sm.load(explicit)
        if not state or state.meta.scope_key != scope:
            raise ValueError("invalid session_id")
        sm.bind_scope(scope, explicit)
        if state.meta.title in _DEFAULT_TITLES and title_hint:
            from session_store import _title_from_hint

            sm.patch_meta(explicit, title=_title_from_hint(title_hint))
        return explicit
    bound = sm.resolve_session_id(scope)
    if bound:
        state = sm.load(bound)
        if state and state.meta.title in _DEFAULT_TITLES and title_hint:
            from session_store import _title_from_hint

            sm.patch_meta(bound, title=_title_from_hint(title_hint))
        return bound
    # pending → create
    from session_store import _title_from_hint

    title = _title_from_hint(title_hint) if title_hint else DEFAULT_TITLE
    state = sm.create_session("assistant_dm", scope, title=title)
    return state.meta.session_id


def purge_all_for_assistant(template_id: str) -> None:
    sm = get_sessions()
    scope = scope_for(template_id)
    metas = sm.list_sessions_for_scope(scope, include_empty=True, include_archived=True)
    from server.runtime.run_coordinator import coordinator

    for meta in metas:
        coordinator.cancel_session(meta.session_id)
        sm.purge_session(meta.session_id)
    sm.unbind_scope(scope)
    repos.delete_all_assistant_messages(template_id)


def sync_after_message(template_id: str, session_id: str) -> None:
    sm = get_sessions()
    _sync_one(sm, template_id, session_id)
