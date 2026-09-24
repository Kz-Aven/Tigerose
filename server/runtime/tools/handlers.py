"""Build tool name → handler callables for a TurnContext."""

from __future__ import annotations

from typing import Any, Callable

from server.runtime.context import TurnContext
from server.runtime.executor import ToolResult
from server.runtime.tools import (
    board,
    cron,
    excel,
    files,
    questions,
    session_ops,
    teammates,
    web,
    worktree,
)
from server.runtime.tools.registry import DANGEROUS_TOOLS, TOOL_SPECS


Handler = Callable[..., str]


def build_handlers(
    ctx: TurnContext,
    *,
    allow_names: list[str] | None = None,
    client=None,
    model: str = "",
    max_tokens: int = 4000,
    extra_kwargs: dict[str, Any] | None = None,
) -> tuple[dict[str, Handler], frozenset[str]]:
    """Return (handlers, dangerous_names)."""
    allowed = set(allow_names) if allow_names is not None else set(TOOL_SPECS.keys())

    def _task(prompt: str, description: str = "") -> str:
        from server.runtime.tools.delegate import run_subagent

        if client is None:
            return "task tool unavailable (no LLM client in context)"
        return run_subagent(
            ctx=ctx,
            client=client,
            model=model,
            prompt=prompt,
            description=description,
            max_tokens=max_tokens,
            extra_kwargs=extra_kwargs,
        )

    all_handlers: dict[str, Handler] = {
        "ask_user_question": lambda questions, _tool_call_id="": questions_tool(
            ctx, questions, _tool_call_id
        ),
        "request_clear_session": lambda reason="": _request_clear_session(ctx, reason),
        "read_file": lambda path, limit=None, offset=None: files.read_file(
            ctx.cwd, path, limit=limit, offset=offset, trusted_roots=ctx.trusted_roots
        ),
        "write_file": lambda path, content: files.write_file(
            ctx.cwd, path, content, trusted_roots=ctx.trusted_roots
        ),
        "edit_file": lambda path, old_text, new_text: files.edit_file(
            ctx.cwd, path, old_text, new_text, trusted_roots=ctx.trusted_roots
        ),
        "glob": lambda pattern: files.glob_files(ctx.cwd, pattern),
        "bash": lambda command, run_in_background=False: _run_bash(ctx, command, run_in_background),
        "bash_status": lambda job_id: _background(ctx, "status", job_id=job_id),
        "bash_wait": lambda job_id, timeout_s=20: _background(ctx, "wait", job_id=job_id, timeout_s=timeout_s),
        "bash_cancel": lambda job_id: _background(ctx, "cancel", job_id=job_id),
        "get_skill": lambda name: session_ops.get_skill(ctx, name),
        "remember": lambda body, type="unknown", summary="": session_ops.remember(ctx, body, type=type, summary=summary),
        "todo_write": lambda todos: session_ops.todo_write(ctx, todos),
        "compact": lambda: session_ops.compact(ctx),
        "task": _task,
        "create_task": lambda subject, description="", blocked_by=None, bind_to_goal=None: _create_task(
            ctx,
            subject,
            description,
            blocked_by,
            bind_to_goal=bind_to_goal,
        ),
        "list_tasks": lambda: board.format_list(ctx.board_scope),
        "get_task": lambda task_id: board.format_one(task_id),
        "claim_task": lambda task_id, owner="agent": _change_task(
            ctx, "claim", task_id, owner=owner or "agent"
        ),
        "complete_task": lambda task_id: _change_task(ctx, "complete", task_id),
        "fail_task": lambda task_id, reason="": _change_task(
            ctx, "fail", task_id, reason=reason or ""
        ),
        "cancel_task": lambda task_id, reason="": _change_task(
            ctx, "cancel", task_id, reason=reason or ""
        ),
        "release_task": lambda task_id: _change_task(ctx, "release", task_id),
        "delete_task": lambda task_id: _change_task(ctx, "delete", task_id),
        "archive_completed_tasks": lambda older_than_days=7: board.archive_completed(
            ctx.board_scope, older_than_days=int(older_than_days or 7)
        ),
        "schedule_cron": lambda prompt, cron_expr="", delay_seconds=None, recurring=None: _schedule(
            ctx, prompt, cron_expr, delay_seconds, recurring
        ),
        "list_crons": lambda: cron.format_jobs(ctx.template_id),
        "cancel_cron": lambda job_id: cron.cancel_job(job_id, template_id=ctx.template_id),
        "spawn_teammate": lambda name, prompt, requires_plan=False: teammates.spawn_teammate(
            ctx, name, prompt, requires_plan=bool(requires_plan)
        ),
        "send_message": lambda to, content: teammates.send_message(
            "lead", to, content, template_id=ctx.template_id
        ),
        "check_inbox": lambda name="lead": teammates.check_inbox(
            name or "lead", template_id=ctx.template_id
        ),
        "request_shutdown": lambda name: teammates.request_shutdown(
            name, template_id=ctx.template_id
        ),
        "request_plan": lambda name, plan: teammates.request_plan(
            name, plan, template_id=ctx.template_id
        ),
        "review_plan": lambda name, approve, note="": teammates.review_plan(
            name, bool(approve), note=note or "", template_id=ctx.template_id
        ),
        "create_worktree": lambda name, task_id="": worktree.create_worktree(
            name, task_id=task_id or "", expected_scope=ctx.board_scope
        ),
        "remove_worktree": lambda name, discard_changes=False: worktree.remove_worktree(
            name, discard_changes=bool(discard_changes)
        ),
        "keep_worktree": lambda name: worktree.keep_worktree(name),
        "web_search": lambda query, limit=5: web.web_search(query, limit=limit),
        "web_extract": lambda urls=None, url="": web.web_extract(urls=urls, url=url or ""),
        "excel_read": lambda path, sheet=None, range=None, max_rows=None, format="markdown": excel.excel_read(
            ctx.cwd,
            path,
            sheet=sheet,
            range=range,
            max_rows=max_rows,
            format=format or "markdown",
            trusted_roots=ctx.trusted_roots,
        ),
        "excel_write": lambda path, mode="set_cells", sheet=None, cells=None, rows=None, range=None, prefer="inplace": excel.excel_write(
            ctx.cwd,
            path,
            sheet=sheet,
            mode=mode or "set_cells",
            cells=cells,
            rows=rows,
            range=range,
            prefer=prefer or "inplace",
            trusted_roots=ctx.trusted_roots,
        ),
    }

    # Accept both cron and cron_expr from model JSON
    def schedule_cron_compat(prompt, cron="", cron_expr="", delay_seconds=None, recurring=None):
        return _schedule(ctx, prompt, cron or cron_expr, delay_seconds, recurring)

    all_handlers["schedule_cron"] = schedule_cron_compat

    from server.runtime.tools.mesh import handlers as mesh_handlers

    if allowed.intersection({"discover_agents", "get_agent_task", "delegate_agent", "send_task_message"}):
        all_handlers.update(mesh_handlers(ctx))
    handlers = {k: v for k, v in all_handlers.items() if k in allowed}
    dangerous = frozenset(n for n in handlers if n in DANGEROUS_TOOLS)
    return handlers, dangerous


def _run_bash(ctx: TurnContext, command: str, run_in_background=False):
    from server.connectors.runtime import execute_managed_cli

    managed = execute_managed_cli(command, ctx.connector_set)
    if managed is not None:
        return managed
    return _background(ctx, "start", command=command) if run_in_background else files.run_bash(ctx.cwd, command)


def _background(ctx, operation, **kwargs):
    import json
    from server.runtime import background_bash
    try:
        value = getattr(background_bash, operation)(ctx, **kwargs)
        return ToolResult(json.dumps(value, ensure_ascii=False), "error" if value['status'] in {'failed', 'timeout'} else "ok")
    except PermissionError as exc:
        return ToolResult(str(exc), "denied")
    except (KeyError, OSError, ValueError) as exc:
        return ToolResult(str(exc), "error")


def questions_tool(ctx, values, tool_call_id):
    return questions.ask_user_question(
        ctx,
        values,
        tool_call_id=tool_call_id,
    )


def _request_clear_session(ctx: TurnContext, reason: str = "") -> Any:
    """Clear after the permission gate already obtained user approval."""
    from server.runtime.session_actors import ACTOR_USER_APPROVED_AGENT
    from server.api import assistant_session_ops as sess

    reason_text = (reason or "助理请求清空当前对话").strip()[:500]
    out = sess.clear_current(
        ctx.template_id,
        actor=ACTOR_USER_APPROVED_AGENT,
        reason=f"request_clear_session:{reason_text[:120]}",
    )
    if out.get("error") == "forbidden":
        return ToolResult(
            f"Clear forbidden: {out.get('detail')}",
            "denied",
            {"tool_name": "request_clear_session"},
        )
    return ToolResult(
        "Session cleared after user approval. An immutable archive was written first.",
        "ok",
        {"tool_name": "request_clear_session", "cleared": True},
    )


def _schedule(ctx, prompt, cron_expr, delay_seconds, recurring):
    result = cron.schedule_job(
        prompt=prompt,
        template_id=ctx.template_id,
        scope_key=ctx.scope_key,
        group_id=ctx.group_id,
        instance_id=ctx.instance_id,
        cron=cron_expr or "",
        delay_seconds=delay_seconds,
        recurring=recurring,
    )
    if isinstance(result, str):
        return f"Error: {result}"
    when = (
        f"in {delay_seconds}s"
        if delay_seconds is not None
        else f"cron '{result.cron}'"
    )
    return f"Scheduled {result.id}: {when} → {prompt[:80]}"


def _create_task(ctx, subject, description="", blocked_by=None, bind_to_goal=None):
    from server.runtime.goal import should_bind_task_to_goal

    goal = (
        ctx.session_state.context.get("goal")
        if ctx.session_state is not None and isinstance(ctx.session_state.context, dict)
        else None
    )
    bind = should_bind_task_to_goal(
        subject or "",
        bind_to_goal=None if bind_to_goal is None else bool(bind_to_goal),
    )
    goal_id = ""
    if bind and isinstance(goal, dict):
        goal_id = str(goal.get("goal_id") or "")
    t = board.create_task(
        ctx.board_scope,
        subject,
        description=description or "",
        blocked_by=list(blocked_by or []),
        goal_id=goal_id,
        run_id=ctx.run_id,
    )
    transition = {
        "ok": True,
        "task_id": t["task_id"],
        "scope_key": t["scope_key"],
        "old_status": None,
        "new_status": t["status"],
        "title": t.get("title") or subject,
    }
    return ToolResult(
        f"Created {t['task_id']}: {t['title']}",
        "ok",
        {
            "task_transition": transition,
            "bind_to_goal": bind,
            "task_title": t.get("title") or subject,
        },
    )


def _change_task(ctx, action: str, task_id: str, **kwargs):
    before = board.get_task(task_id)
    if before and before.get("scope_key") != ctx.board_scope:
        transition = {
            "ok": False,
            "task_id": task_id,
            "scope_key": before.get("scope_key"),
            "old_status": before.get("status"),
            "new_status": before.get("status"),
        }
        return ToolResult(
            f"Task scope mismatch: expected {ctx.board_scope}, found {before.get('scope_key')}",
            "denied",
            {"task_transition": transition},
        )
    goal = (
        ctx.session_state.context.get("goal")
        if ctx.session_state is not None and isinstance(ctx.session_state.context, dict)
        else None
    )
    goal_id = str(goal.get("goal_id") or "") if isinstance(goal, dict) else ""
    if action == "claim":
        content = board.claim_task(
            task_id,
            owner=kwargs.get("owner") or "agent",
            expected_scope=ctx.board_scope,
            goal_id=goal_id,
            run_id=ctx.run_id,
        )
    elif action == "complete":
        from server.runtime.acceptance import assert_plugin_install_for_texts

        denial = assert_plugin_install_for_texts(
            str((before or {}).get("title") or ""),
            str((before or {}).get("description") or ""),
            str((goal or {}).get("condition") or "") if isinstance(goal, dict) else "",
        )
        if denial:
            transition = {
                "ok": False,
                "task_id": task_id,
                "scope_key": (before or {}).get("scope_key", ctx.board_scope),
                "old_status": (before or {}).get("status"),
                "new_status": (before or {}).get("status"),
            }
            return ToolResult(
                denial,
                "denied",
                {
                    "task_transition": transition,
                    "acceptance_ok": False,
                    "recoverable_deny": True,
                },
            )
        content = board.complete_task(task_id, expected_scope=ctx.board_scope)
    elif action == "fail":
        content = board.fail_task(
            task_id, kwargs.get("reason") or "", expected_scope=ctx.board_scope
        )
    elif action == "cancel":
        content = board.cancel_task(
            task_id, kwargs.get("reason") or "", expected_scope=ctx.board_scope
        )
    elif action == "release":
        content = board.release_task(task_id, expected_scope=ctx.board_scope)
    else:
        content = board.delete_task(task_id, expected_scope=ctx.board_scope)
    after = board.get_task(task_id)
    ok = not any(
        marker in content.lower()
        for marker in ("not found", "scope mismatch", "cannot ", "expected ", "blocked by")
    )
    transition = {
        "ok": ok,
        "task_id": task_id,
        "scope_key": (after or before or {}).get("scope_key", ctx.board_scope),
        "old_status": (before or {}).get("status"),
        "new_status": (after or {}).get("status", "missing"),
    }
    return ToolResult(
        content,
        "ok" if ok else "error",
        {"task_transition": transition},
    )
