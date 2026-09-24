"""Session-scoped tools: todo_write, compact, remember, get_skill."""

from __future__ import annotations

import json
from typing import Any

from server.capabilities.catalog import format_skill_context, load_skill_texts
from server.db import repos
from server.runtime.context import TurnContext


def get_skill(ctx: TurnContext, name: str) -> str:
    candidates = list(ctx.enabled_skill_ids)
    exact = [sid for sid in candidates if name == sid]
    short = [sid for sid in candidates if sid.endswith(f":skill:{name}") or sid.endswith(f"/{name}")]
    matches = exact or short
    if not matches:
        return f"Skill not enabled or not found: {name}. Enabled: {', '.join(candidates) or '(none)'}"
    if len(matches) > 1:
        return f"Skill name is ambiguous: {name}. Use one of: {', '.join(matches)}"
    matched = matches[0]
    connector_path = ctx.connector_skills.get(matched)
    if connector_path:
        try:
            return format_skill_context({
                "id": matched,
                "path": str(connector_path),
                "content": connector_path.read_text(encoding="utf-8")[:20000],
            })
        except OSError:
            return f"Skill file missing: {matched}"
    texts = load_skill_texts([matched], workspace=ctx.cwd)
    if not texts:
        return f"Skill file missing: {matched}"
    return format_skill_context(texts[0])


def remember(ctx: TurnContext, body: str, type: str = "unknown", summary: str | None = None) -> str:
    body = (body or "").strip()
    if not body:
        return "Empty memory body"
    try:
        mem = repos.add_assistant_memory(ctx.template_id, body, source="tool", type=type, summary=summary,
            scope_kind="group" if ctx.group_id else "assistant", scope_id=ctx.group_id or ctx.template_id,
            source_groups=[ctx.group_id] if ctx.group_id else [],
            source_refs=[{"kind": "run", "id": ctx.run_id, "session_id": ctx.session_id}])
    except ValueError as exc:
        return f"Memory not saved: {exc}"
    return f"Remembered ({mem['memory_id']}): {body[:200]}"


def todo_write(ctx: TurnContext, todos: list[dict[str, Any]]) -> str:
    from server.runtime.executor import ToolResult

    state = ctx.session_state
    sm = ctx.session_manager
    if state is None or sm is None:
        return ToolResult("No active session for todos", "error")
    cleaned = []
    for i, t in enumerate(todos or []):
        if not isinstance(t, dict):
            continue
        cleaned.append(
            {
                "id": str(t.get("id") or f"todo_{i+1}"),
                "content": str(t.get("content") or ""),
                "status": str(t.get("status") or "pending"),
            }
        )
    state.todos = cleaned
    goal = state.context.get("goal") if isinstance(state.context, dict) else None
    if isinstance(goal, dict) and goal.get("status") == "active":
        baseline = set(goal.get("todo_baseline") or [])
        bound = goal.setdefault("todo_ids", [])
        for todo in cleaned:
            todo_id = todo["id"]
            if todo_id not in baseline and todo_id not in bound:
                bound.append(todo_id)
        goal["updated_at"] = __import__("time").time()
    sm.save(state, expected_version=state.version, retries=5)
    lines = [f"- [{t['status']}] {t['content']}" for t in cleaned]
    return ToolResult(
        "Todos updated:\n" + ("\n".join(lines) if lines else "(empty)"),
        "ok",
        {"todo_ids": [t["id"] for t in cleaned]},
    )


def compact(ctx: TurnContext) -> str:
    """Preserve original user evidence while replacing complete middle turns."""
    from server.runtime.session_compaction import compact_history

    state = ctx.session_state
    sm = ctx.session_manager
    if state is None or sm is None:
        return "No active session to compact"
    proposal = compact_history(state.messages, state.compact_summary)
    if not proposal["changed"]:
        return "Cannot compact: " + proposal["reason"]
    old_messages, old_summary = state.messages, state.compact_summary
    state.messages, state.compact_summary = proposal["messages"], proposal["summary"]
    try:
        sm.save(state, expected_version=state.version, retries=5)
    except Exception:
        state.messages, state.compact_summary = old_messages, old_summary
        raise
    return f"Compacted {proposal['removed_messages']} messages. Original user evidence preserved."
