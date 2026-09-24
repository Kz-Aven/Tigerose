"""One-shot sub-agent via task tool."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from openai import OpenAI

from server.runtime.context import TurnContext
from server.runtime.tools.registry import SUBAGENT_TOOLS, schemas_for_names


def run_subagent(
    *,
    ctx: TurnContext,
    client: OpenAI,
    model: str,
    prompt: str,
    description: str = "",
    max_tokens: int = 4000,
    extra_kwargs: dict[str, Any] | None = None,
) -> Any:
    from avent_config import cfg_get, get_config
    from server.db import repos
    from server.runtime.executor import ToolExecutor, ToolResult
    from server.runtime.loop import run_tool_loop
    from server.runtime.policy import parse_permission_policy
    from server.runtime.tools.handlers import build_handlers

    sub_ctx = TurnContext(
        template_id=ctx.template_id,
        scope_key=f"{ctx.scope_key}:subtask",
        session_id=ctx.session_id,
        cwd=Path(ctx.cwd),
        surface="subagent",
        trusted_roots=ctx.trusted_roots,
        group_id=ctx.group_id,
        instance_id=ctx.instance_id,
        enabled_skill_ids=list(ctx.enabled_skill_ids),
        connector_skills=dict(ctx.connector_skills),
        connector_set=ctx.connector_set,
        session_manager=ctx.session_manager,
        session_state=ctx.session_state,
        run_id=ctx.run_id,
        cancel_token=ctx.cancel_token,
        publish=ctx.publish,
    )
    names = sorted(SUBAGENT_TOOLS)
    schemas = schemas_for_names(names)
    handlers, dangerous = build_handlers(sub_ctx, allow_names=names)
    executor = ToolExecutor(
        cwd=sub_ctx.cwd,
        safe_handlers={k: v for k, v in handlers.items() if k not in dangerous},
        dangerous_handlers={k: v for k, v in handlers.items() if k in dangerous},
        all_handlers=handlers,
        timeout_s=__import__(
            "server.runtime.loop", fromlist=["configured_tool_timeout_s"]
        ).configured_tool_timeout_s(),
        trusted_roots=sub_ctx.trusted_roots,
    )
    system = (
        "You are a one-shot sub-agent. Complete the task and reply with a concise summary. "
        "Do not spawn further agents. Prefer tools when needed.\n"
        f"Workspace root (authoritative): {sub_ctx.cwd}\n"
        "Use paths relative to this workspace. Do not invent another absolute project path."
        " If the user supplies a path outside this workspace, call the relevant file tool with "
        "that path so the user can approve access; do not skip the work or bypass the gate with shell."
    )
    if sub_ctx.trusted_roots:
        system += (
            "\nTrusted application data directories:\n"
            + "\n".join(f"- {root}" for root in sub_ctx.trusted_roots)
            + "\nUse absolute paths within these directories without requesting access."
        )
    try:
        from agents_md import agents_md_for_prompt

        agents_rules = agents_md_for_prompt(full=False)
        if agents_rules:
            system = f"{system}\n\n{agents_rules}"
    except Exception:
        pass
    user = prompt if not description else f"{description}\n\n{prompt}"
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    try:
        max_rounds = int(
            cfg_get(get_config(), "delegation", "max_iterations", default=50)
        )
    except (TypeError, ValueError):
        max_rounds = 50
    max_rounds = max(1, min(max_rounds, 50))
    template = repos.get_template(ctx.template_id) or {}
    result = run_tool_loop(
        client=client,
        model=model,
        messages=messages,
        schemas=schemas,
        executor=executor,
        max_tokens=max_tokens,
        extra_kwargs=extra_kwargs,
        max_rounds=max_rounds,
        cancel_check=ctx.cancel_token.checkpoint if ctx.cancel_token else None,
        permission_channel=ctx.sse_channel,
        permission_policy=parse_permission_policy(template.get("config_meta") or {}),
    )
    reply = (result.get("reply") or "").strip()
    termination = str(result.get("termination") or "normal_stop")
    status = {
        "normal_stop": "completed",
        "max_turns": "max_rounds",
        "llm_error": "error",
        "cancelled": "cancelled",
    }.get(termination, termination)
    outcome = "ok" if status == "completed" else (
        "cancelled" if status == "cancelled" else "error"
    )
    content = reply[:4000] or "(sub-agent returned empty summary)"
    if status != "completed":
        content = (
            f"Sub-agent failed ({status}, rounds={result.get('rounds', 0)}/{max_rounds}). "
            f"{content}"
        )
    return ToolResult(
        content,
        outcome,
        {
            "subagent_status": status,
            "subagent_rounds": int(result.get("rounds") or 0),
            "subagent_max_rounds": max_rounds,
            "workspace": str(sub_ctx.cwd),
        },
    )
