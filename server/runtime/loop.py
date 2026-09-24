"""Avent Agent tool-calling loop (logical instance, shared engine)."""

from __future__ import annotations

import json
import hashlib
import logging
import re
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from openai import OpenAI

from avent_config import cfg_get, get_config
from server.runtime.context import TurnContext
from server.runtime.executor import ToolExecutor, ToolResult
from server.runtime.goal import READONLY_CACHE_TOOLS
from server.runtime.mcp import connect_mcp_servers, mcp_tool_openai
from server.runtime.plugins import load_plugin_prompt_fragments, load_plugin_tool_handlers
from server.runtime.tools.handlers import build_handlers
from server.runtime.tools.registry import resolve_tool_kind, schemas_for_names

log = logging.getLogger("avent.loop")

# Fallback if config missing; config.yaml agent.max_turns is the source of truth.
_DEFAULT_MAX_ROUNDS = 150
_PREVIEW_CHARS = 500
_MAX_THINKING_STEPS = 300
_FORCE_TOOLS_NUDGE = (
    "You described a plan or asked for confirmation, but you did not call any tools. "
    "Execute now: call the appropriate tools (e.g. get_skill, read_file, bash). "
    "Do not ask the user to confirm. Do not only restate the plan."
)
_CONTINUATION_NUDGE = (
    "Continue from the previous response. Do not repeat completed work. "
    "Call tools if more work remains; otherwise give the final answer."
)
_BUDGET_CONVERGENCE_NUDGE = (
    "You are approaching the run budget limit. Converge now: finish the user's "
    "objective with only essential actions — no new diagnostics or scope expansion. "
    "If you already have terminal evidence (e.g. a sent-message receipt), stop and summarize."
)
_BUDGET_EXHAUSTED_REPLY = (
    "This run reached its budget limit (rounds, tool calls, or time). "
    "Here is what was completed; further work requires a new run."
)

TraceEventFn = Callable[[dict[str, Any]], None]


def configured_max_tool_rounds() -> int:
    try:
        n = int(cfg_get(get_config(), "agent", "max_turns", default=_DEFAULT_MAX_ROUNDS))
    except Exception:
        n = _DEFAULT_MAX_ROUNDS
    return max(1, min(n, 500))


def configured_tool_timeout_s() -> float:
    try:
        value = float(cfg_get(get_config(), "agent", "tool_timeout_s", default=180))
    except (TypeError, ValueError):
        value = 180.0
    return max(1.0, value)


def preview_text(value: Any, limit: int = _PREVIEW_CHARS) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except Exception:
            text = str(value)
    else:
        text = str(value)
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def make_trace_step(
    kind: str,
    name: str,
    detail: str = "",
    *,
    ts: float | None = None,
) -> dict[str, Any]:
    return {
        "kind": kind,
        "name": name,
        "detail": preview_text(detail),
        "ts": float(ts if ts is not None else time.time()),
    }


def build_tool_surface(
    *,
    bundle: dict[str, Any],
    ctx: TurnContext,
    client: OpenAI | None = None,
    model: str = "",
    max_tokens: int = 4000,
    extra_kwargs: dict[str, Any] | None = None,
    web_enabled: bool = False,
) -> dict[str, Any]:
    tool_names = [
        t.get("name") or str(t.get("id", "")).removeprefix("tool:")
        for t in bundle.get("tools") or []
        if not t.get("missing")
    ]
    if "ask_user_question" not in tool_names:
        tool_names.append("ask_user_question")
    if "request_clear_session" not in tool_names:
        tool_names.append("request_clear_session")
    if "bash" in tool_names:
        tool_names.extend(name for name in ("bash_status", "bash_wait", "bash_cancel") if name not in tool_names)
    from server.runtime.feature_flags import flag_enabled
    from server.runtime.tools.mesh import MESH_TOOL_SPECS

    if flag_enabled("agent_mesh_v1") and ctx.surface not in {"subagent", "teammate"}:
        tool_names.extend(name for name in MESH_TOOL_SPECS if name not in tool_names)
        # Existing assistants must use the durable mesh contract. The legacy
        # in-process teammate cannot create a reviewable collaboration task.
        tool_names = [name for name in tool_names if name != "spawn_teammate"]
    else:
        tool_names = [name for name in tool_names if name not in MESH_TOOL_SPECS]
    if ctx.mesh_task_id:
        from server.db import mesh_repos
        if mesh_repos.get_task(ctx.mesh_task_id, actor_id=ctx.template_id)['executor_kind'] == 'mesh_coordination':
            tool_names = [name for name in tool_names if name != 'submit_agent_task']
        tool_names = [name for name in tool_names if name not in {
            "request_clear_session", "remember", "schedule_cron", "spawn_teammate", "task",
            "send_message", "check_inbox", "request_plan", "review_plan", "request_shutdown",
        }]
    # Drop unknown / non-executable names (e.g. legacy connect_mcp)
    from server.runtime.tools.registry import TOOL_SPECS
    from server.runtime.web.registry import WEB_TOOL_NAMES

    tool_names = [n for n in tool_names if n in TOOL_SPECS]
    # Two-layer gate: capabilities already filtered into tool_names;
    # web_enabled must also be true or web_* are stripped.
    if not web_enabled:
        tool_names = [n for n in tool_names if n not in WEB_TOOL_NAMES]

    plugin_ids = [p["id"] for p in bundle.get("plugins") or [] if not p.get("missing")]
    mcp_ids = [m["id"] for m in bundle.get("mcp_servers") or [] if not m.get("missing")]
    skill_ids = [s["id"] for s in bundle.get("skills") or [] if not s.get("missing")]
    ctx.enabled_skill_ids = list(dict.fromkeys([*skill_ids, *ctx.connector_skills]))
    from server.capabilities.catalog import load_skill_texts

    # Only currently enabled and resolved skills grant access to their resource roots.
    skill_paths = [
        Path(skill["path"])
        for skill in load_skill_texts(skill_ids, workspace=ctx.cwd)
    ]
    skill_paths.extend(path for path in ctx.connector_skills.values() if path.is_file())
    skill_roots = tuple(dict.fromkeys(path.resolve().parent for path in skill_paths))

    mcp = connect_mcp_servers(
        mcp_ids,
        workspace=ctx.cwd,
        timeout=configured_tool_timeout_s(),
    )
    clients = mcp["clients"]
    # Alias/display names resolve calls; never iterate them for OpenAI schemas.
    mcp_lookup = dict(mcp.get("lookup") or clients)
    warnings = list(mcp["warnings"])

    schemas = schemas_for_names(tool_names)
    seen_tool_names: set[str] = {
        str(s.get("function", {}).get("name") or "") for s in schemas
    }
    for server, mcp_client in clients.items():
        for td in mcp_client.tools:
            schema = mcp_tool_openai(server, td)
            wire_name = str(schema.get("function", {}).get("name") or "")
            if not wire_name or wire_name in seen_tool_names:
                continue
            seen_tool_names.add(wire_name)
            schemas.append(schema)

    plugin_schemas, plugin_handlers = load_plugin_tool_handlers(
        plugin_ids, workspace=ctx.cwd
    )
    for schema in plugin_schemas:
        wire_name = str((schema.get("function") or {}).get("name") or "")
        if not wire_name or wire_name in seen_tool_names:
            continue
        seen_tool_names.add(wire_name)
        schemas.append(schema)

    handlers, dangerous = build_handlers(
        ctx,
        allow_names=tool_names,
        client=client,
        model=model,
        max_tokens=max_tokens,
        extra_kwargs=extra_kwargs,
    )
    executor = ToolExecutor(
        cwd=Path(ctx.cwd),
        safe_handlers={k: v for k, v in handlers.items() if k not in dangerous},
        dangerous_handlers={k: v for k, v in handlers.items() if k in dangerous},
        all_handlers=handlers,
        mcp_clients=mcp_lookup,
        plugin_handlers=plugin_handlers,
        timeout_s=configured_tool_timeout_s(),
        skill_roots=skill_roots,
        trusted_roots=ctx.trusted_roots,
        connector_executables=ctx.connector_set.executable_names if ctx.connector_set else (),
    )
    plugin_prompts = load_plugin_prompt_fragments(plugin_ids, workspace=ctx.cwd)

    return {
        "schemas": schemas,
        "executor": executor,
        "warnings": warnings,
        "plugin_prompts": plugin_prompts,
        "enabled": bool(schemas),
        "mcp_connected": list(clients.keys()),
        "tool_names": [s["function"]["name"] for s in schemas],
    }


def run_tool_loop(
    *,
    client: OpenAI,
    model: str,
    messages: list[dict[str, Any]],
    schemas: list[dict[str, Any]],
    executor: ToolExecutor,
    max_tokens: int,
    extra_kwargs: dict[str, Any] | None = None,
    max_rounds: int | None = None,
    on_event: TraceEventFn | None = None,
    on_intermediate_content: Callable[[str, list[dict[str, Any]]], None] | None = None,
    permission_channel: str | None = None,
    permission_policy: Any | None = None,
    goal_controller: Any | None = None,
    cancel_check: Callable[[], None] | None = None,
    run_scope: Any | None = None,
    run_budget: Any | None = None,
    task_intent: str | None = None,
    run_objective: Any | None = None,
    session_state: Any | None = None,
    command_authorization: dict[str, Any] | None = None,
    absorb_into_goal: bool = True,
    usage_context: dict[str, Any] | None = None,
    user_hooks: list[dict[str, Any]] | None = None,
    hook_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Multi-round chat.completions with tools. Returns final text + tool/thinking traces."""
    extra_kwargs = dict(extra_kwargs or {})
    if max_rounds is None:
        max_rounds = configured_max_tool_rounds()
    api_messages = list(messages)
    trace: list[dict[str, Any]] = []
    thinking_steps: list[dict[str, Any]] = []
    final_text = ""
    content = ""
    forced_tools_once = False
    length_continues = 0
    t0 = time.time()

    from server.runtime.policy import parse_permission_policy
    from server.runtime.tools import files as file_tools

    policy = permission_policy or parse_permission_policy({})
    mon_tok = file_tools.set_monitor_dangerous_bash(policy.monitors_dangerous_bash())
    try:
        return _run_tool_loop_inner(
            client=client,
            model=model,
            api_messages=api_messages,
            schemas=schemas,
            executor=executor,
            max_tokens=max_tokens,
            extra_kwargs=extra_kwargs,
            max_rounds=max_rounds,
            on_event=on_event,
            on_intermediate_content=on_intermediate_content,
            permission_channel=permission_channel,
            policy=policy,
            goal_controller=goal_controller,
            cancel_check=cancel_check,
            run_scope=run_scope,
            run_budget=run_budget,
            task_intent=task_intent,
            run_objective=run_objective,
            session_state=session_state,
            command_authorization=command_authorization,
            absorb_into_goal=absorb_into_goal,
            usage_context=usage_context or {},
            user_hooks=user_hooks or [],
            hook_payload=hook_payload or {},
            emit_init=(trace, thinking_steps, t0),
        )
    finally:
        file_tools.reset_monitor_dangerous_bash(mon_tok)


def _run_tool_loop_inner(
    *,
    client: OpenAI,
    model: str,
    api_messages: list[dict[str, Any]],
    schemas: list[dict[str, Any]],
    executor: ToolExecutor,
    max_tokens: int,
    extra_kwargs: dict[str, Any],
    max_rounds: int,
    on_event: TraceEventFn | None,
    on_intermediate_content: Callable[[str, list[dict[str, Any]]], None] | None,
    permission_channel: str | None,
    policy: Any,
    goal_controller: Any | None,
    cancel_check: Callable[[], None] | None,
    run_scope: Any | None,
    run_budget: Any | None,
    task_intent: str | None,
    run_objective: Any | None,
    session_state: Any | None,
    command_authorization: dict[str, Any] | None,
    absorb_into_goal: bool,
    usage_context: dict[str, Any],
    user_hooks: list[dict[str, Any]],
    hook_payload: dict[str, Any],
    emit_init: tuple,
) -> dict[str, Any]:
    from server.runtime.feature_flags import flag_enabled

    trace, thinking_steps, t0 = emit_init
    final_text = ""
    content = ""
    forced_tools_once = False
    length_continues = 0
    termination = "normal_stop"
    rounds_used = 0
    call_attempts: dict[str, int] = {}
    denied_calls: set[str] = set()
    in_doubt_calls: set[str] = set()
    # Successful readonly results within this run — repeats return cache.
    readonly_cache: dict[str, ToolResult] = {}
    soft_nudged = False
    acceptance_decision: Any | None = None
    stop_run = False
    budget_guard = bool(run_budget is not None and flag_enabled("budget_guard_v2"))
    tool_semantics_on = flag_enabled("tool_semantics_v2")
    attempt_ledger_on = flag_enabled("attempt_ledger_v2")
    domain_acceptance_on = flag_enabled("domain_acceptance_v2")
    display_thinking_start = 0
    last_failed_call: dict[str, str] | None = None
    history_prefix_end = next((i for i, message in enumerate(api_messages)
                               if message.get("tool_calls")), len(api_messages))
    input_suffix_count = len(api_messages) - history_prefix_end
    history_summary = getattr(session_state, "compact_summary", "")
    initial_question = (getattr(session_state, "context", {}).get("pending_question_continuation") or {}).get("question_id")
    resumed_question = initial_question if input_suffix_count else None
    latest_rich_input = next((m for m in reversed(api_messages[:history_prefix_end])
                              if m.get("role") == "user"), None)
    if latest_rich_input and not isinstance(latest_rich_input.get("content"), list):
        latest_rich_input = None

    def append_audit(stage: str, **fields: Any) -> None:
        run_id = str(getattr(goal_controller, "run_id", "") or "")
        if not run_id:
            return
        try:
            from server.runtime.run_transcript import append_run_event

            append_run_event(run_id, {"kind": "audit", "stage": stage, **fields})
        except Exception:
            log.exception("run audit append failed")

    def emit(step: dict[str, Any]) -> None:
        nonlocal display_thinking_start
        if goal_controller is not None and getattr(goal_controller, "run_id", ""):
            step.setdefault("run_id", goal_controller.run_id)
        thinking_steps.append(step)
        if len(thinking_steps) > _MAX_THINKING_STEPS:
            removed = len(thinking_steps) - _MAX_THINKING_STEPS
            del thinking_steps[:removed]
            display_thinking_start = max(0, display_thinking_start - removed)
        run_id = str(step.get("run_id") or getattr(goal_controller, "run_id", "") or "")
        if run_id:
            try:
                from server.runtime.run_transcript import append_run_event

                append_run_event(
                    run_id,
                    {
                        "kind": "trace",
                        "trace_kind": step.get("kind"),
                        "name": step.get("name"),
                        "detail": preview_text(step.get("detail") or "", 400),
                    },
                )
            except Exception:
                pass
        if on_event:
            try:
                on_event(step)
            except Exception:
                log.exception("on_event failed")

    def emit_intermediate_content(text: str) -> None:
        """Publish one non-final assistant response with its preceding trace segment."""
        nonlocal display_thinking_start
        if not text or on_intermediate_content is None:
            return
        try:
            on_intermediate_content(text, list(thinking_steps[display_thinking_start:]))
        except Exception:
            log.exception("on_intermediate_content failed")
            return
        display_thinking_start = len(thinking_steps)

    def _budget_summary() -> dict[str, Any]:
        assert run_budget is not None
        return {
            "root_budget_id": run_budget.root_budget_id,
            "profile": run_budget.profile,
            "consumed": dict(run_budget.consumed),
            "reserved": dict(run_budget.reserved),
            "limits": dict(run_budget.limits_snapshot),
        }

    def _loop_return(
        *,
        reply: str | None = None,
        include_messages: bool = True,
        pending_question: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        out: dict[str, Any] = {
            "reply": final_text if reply is None else reply,
            "tool_trace": trace,
            "thinking_steps": thinking_steps,
            "display_thinking_steps": thinking_steps[display_thinking_start:],
            "duration_ms": int((time.time() - t0) * 1000),
            "rounds": rounds_used,
            "termination": termination,
        }
        if include_messages:
            out["api_messages"] = api_messages
            out["history_prefix_end"] = history_prefix_end
            out["input_suffix_count"] = input_suffix_count
        if pending_question is not None:
            out["pending_question"] = pending_question
        if run_budget is not None:
            out["budget"] = _budget_summary()
        if task_intent is not None:
            out["intent"] = task_intent
        if acceptance_decision is not None:
            out["acceptance"] = {
                "status": acceptance_decision.status,
                "objective_id": acceptance_decision.objective_id,
                "reason": acceptance_decision.reason,
                "receipt_ids": list(acceptance_decision.receipt_ids),
            }
        if termination in {
            "accepted",
            "budget_exhausted",
            "blocked_runtime",
            "normal_stop",
            "waiting_for_user",
        }:
            try:
                from server.runtime.run_outcome import map_loop_termination

                reason_map = {
                    "accepted": "accepted",
                    "budget_exhausted": "budget",
                    "blocked_runtime": "runtime_block",
                }
                outcome = map_loop_termination(
                    termination_reason=reason_map.get(termination, termination),
                    objective_id=str(getattr(run_objective, "objective_id", "") or ""),
                    acceptance=acceptance_decision,
                    intent=task_intent or "",
                )
                out["run_outcome_status"] = outcome.status
            except Exception:
                pass
        return out

    def _resolve_tool_semantics(name: str, args: dict[str, Any]) -> tuple[str, Any | None]:
        if tool_semantics_on:
            from server.runtime.tool_semantics import legacy_tool_kind, resolve_tool_semantics

            sem = resolve_tool_semantics(name, args)
            return legacy_tool_kind(sem), sem
        return resolve_tool_kind(name, args), None

    def _merge_semantics_metadata(result: ToolResult, sem: Any | None) -> ToolResult:
        if sem is None:
            return result
        from server.runtime.tool_semantics import legacy_tool_kind

        meta = dict(result.metadata or {})
        meta.update(
            {
                "tool_effect": sem.effect,
                "goal_role": sem.goal_role,
                "domain": sem.domain,
                "operation": sem.operation,
                "acceptance_type": sem.acceptance_type,
                "tool_kind": legacy_tool_kind(sem),
            }
        )
        return ToolResult(result.content, result.outcome, meta)

    if run_objective is not None and goal_controller is not None:
        # A Goal must never inherit failed attempts from another user request.
        goal_controller.bind_objective(run_objective.objective_id)

    def _budget_exhausted(*, detail: str = "") -> dict[str, Any]:
        nonlocal termination, final_text
        termination = "budget_exhausted"
        final_text = _BUDGET_EXHAUSTED_REPLY
        if detail:
            final_text = f"{_BUDGET_EXHAUSTED_REPLY} ({detail})"
        emit(make_trace_step("hook", "BudgetExhausted", detail or termination))
        emit(make_trace_step("hook", "Stop", termination))
        return _loop_return()

    for _round in range(max_rounds):
        # Refresh only between complete tool batches; the active tool protocol
        # remains byte-for-byte intact after the replaced historical prefix.
        if session_state is not None and session_state.compact_summary != history_summary:
            prefix = [m for m in api_messages[:history_prefix_end] if m.get("role") == "system"]
            prefix.extend({"role": m["role"], "content": m["content"]}
                          for m in session_state.messages
                          if m.get("role") in {"user", "assistant"} and isinstance(m.get("content"), str)
                          and not (resumed_question and m.get("question_id") == resumed_question))
            if latest_rich_input:
                text = "".join(p.get("text", "") for p in latest_rich_input["content"] if p.get("type") == "text")
                latest_user = next((m for m in reversed(prefix) if m.get("role") == "user"), None)
                if latest_user and latest_user.get("content") == text:
                    latest_user["content"] = latest_rich_input["content"]
            api_messages[:] = prefix + api_messages[history_prefix_end:]
            history_prefix_end = len(prefix)
            history_summary = session_state.compact_summary
        if stop_run:
            break
        rounds_used = _round + 1
        if cancel_check:
            cancel_check()

        llm_rid: str | None = None
        if budget_guard:
            from server.runtime.budgets import (
                check_hard,
                check_soft,
                commit,
                remaining_timeout_s,
                reserve,
                release,
            )

            if (
                check_hard(run_budget, "llm_round") == "hard"
                or remaining_timeout_s(run_budget) <= 0
            ):
                return _budget_exhausted(
                    detail="llm_round hard limit or timeout before API call"
                )
            if not soft_nudged:
                soft_level = check_soft(run_budget, "llm_round") or check_soft(
                    run_budget, "tool_call"
                )
                if soft_level == "soft":
                    soft_nudged = True
                    api_messages.append(
                        {"role": "user", "content": _BUDGET_CONVERGENCE_NUDGE}
                    )
                    emit(make_trace_step("hook", "BudgetSoftNudge", "converge"))
            llm_rid = f"llm_round_{_round}_{uuid.uuid4().hex[:8]}"
            if not reserve(run_budget.root_budget_id, llm_rid, "llm_round", 1):
                if llm_rid:
                    release(llm_rid)
                return _budget_exhausted(detail="llm_round reserve failed")

        kwargs: dict[str, Any] = {
            "model": model,
            "messages": api_messages,
            "max_tokens": max_tokens,
            **extra_kwargs,
        }
        if schemas:
            kwargs["tools"] = schemas
            kwargs["tool_choice"] = "auto"
        from server.runtime.mesh_runtime import _current_work

        mesh_work = _current_work.get()
        mesh_reservation = None
        if mesh_work:
            from server.runtime import mesh_budget

            allocation = mesh_budget.reserve_llm(
                mesh_work, mesh_budget.estimate_input_tokens(api_messages, schemas), max_tokens,
            )
            if not allocation["allowed"]:
                if budget_guard and llm_rid:
                    from server.runtime.budgets import release
                    release(llm_rid)
                termination = "mesh_yield"
                return _loop_return(reply="")
            mesh_reservation = allocation["reservation_id"]
        try:
            from server.runtime.usage import create_completion

            resp = create_completion(
                client,
                profile=usage_context.get("profile") or {"id": model},
                call_kind="agent_loop",
                run_id=str(usage_context.get("run_id") or ""),
                session_id=str(usage_context.get("session_id") or ""),
                agent_id=str(usage_context.get("agent_id") or ""),
                **kwargs,
            )
        except Exception as e:
            if mesh_reservation:
                mesh_budget.settle_llm(mesh_reservation)
            if budget_guard and llm_rid:
                from server.runtime.budgets import commit

                commit(llm_rid, 1)
            emit(make_trace_step("hook", "Error", str(e)))
            out = _loop_return(reply=f"[LLM error] {e}", include_messages=False)
            out["termination"] = "llm_error"
            return out
        if mesh_reservation:
            usage = getattr(resp, "usage", None)
            mesh_budget.settle_llm(mesh_reservation,
                                  input_tokens=getattr(usage, "prompt_tokens", None),
                                  output_tokens=getattr(usage, "completion_tokens", None))
        if budget_guard and llm_rid:
            from server.runtime.budgets import commit

            commit(llm_rid, 1)
        if cancel_check:
            cancel_check()

        choice = resp.choices[0]
        msg = choice.message
        tool_calls = getattr(msg, "tool_calls", None) or []
        content = (msg.content or "").strip()
        finish = getattr(choice, "finish_reason", None)

        # Truncated text with no tools → continue (CLI parity)
        if finish == "length" and not tool_calls and length_continues < 2:
            length_continues += 1
            log.info("tool loop: finish_reason=length, injecting continuation (%s)", length_continues)
            emit(
                make_trace_step(
                    "hook",
                    "LengthContinue",
                    f"finish_reason=length; retry {length_continues}",
                )
            )
            if content:
                emit_intermediate_content(content)
                api_messages.append({"role": "assistant", "content": content})
            api_messages.append({"role": "user", "content": _CONTINUATION_NUDGE})
            continue

        if not tool_calls:
            # First response is plan/confirmation only → force one tool-calling retry
            if (
                schemas
                and not trace
                and not forced_tools_once
                and content
                and _looks_like_plan_without_action(content)
            ):
                forced_tools_once = True
                log.info("tool loop: text-only plan; nudging model to call tools")
                emit(
                    make_trace_step(
                        "hook",
                        "ForceTools",
                        preview_text(content, 160),
                    )
                )
                emit_intermediate_content(content)
                api_messages.append({"role": "assistant", "content": content})
                api_messages.append({"role": "user", "content": _FORCE_TOOLS_NUDGE})
                continue
            if goal_controller is not None and task_intent != "one_shot_action":
                decision = goal_controller.on_candidate_stop(content)
                action = decision.get("action")
                if action == "block":
                    if content:
                        emit_intermediate_content(content)
                        api_messages.append({"role": "assistant", "content": content})
                    api_messages.append(
                        {"role": "user", "content": str(decision.get("message") or "")}
                    )
                    continue
                if action in {"exhausted", "suspended"}:
                    final_text = str(decision.get("message") or content)
                    break
            final_text = content
            break

        emit_intermediate_content(content)
        api_messages.append(
            {
                "role": "assistant",
                "content": content or None,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments or "{}",
                        },
                    }
                    for tc in tool_calls
                ],
            }
        )

        # Authorize a response's unknown/write shell plan as a group. The
        # response is already recorded above, so the card can show exactly
        # what the model proposed before any command is dispatched.
        from server.runtime.command_plan import (
            classify_bash_command,
            grant_task_group,
            has_task_grant,
        )
        from server.runtime import permissions as plan_permissions
        from server.runtime.mesh_runtime import _current_work

        command_plans: dict[str, Any] = {}
        batch_allowed_calls: set[str] = set()
        batch_denied_calls: set[str] = set()
        batch_groups: dict[str, list[tuple[str, Any]]] = {
            "unknown": [],
            "mutation": [],
        }
        for planned_call in tool_calls:
            if planned_call.function.name != "bash":
                continue
            try:
                planned_args = json.loads(planned_call.function.arguments or "{}")
            except json.JSONDecodeError:
                planned_args = {}
            if not isinstance(planned_args, dict):
                planned_args = {}
            from server.runtime.scope_guard import _is_connector_cli_command

            if _is_connector_cli_command(
                str(planned_args.get("command") or ""),
                list(executor.connector_executables),
            ):
                continue
            plan = classify_bash_command(str(planned_args.get("command") or ""))
            command_plans[planned_call.id] = plan
            if (
                command_authorization is not None
                # Durable task approvals must freeze actual tool arguments.
                # Numbered batch display text is not an executable command.
                and _current_work.get() is None
                and
                plan.risk in batch_groups
                and not has_task_grant(command_authorization or {}, plan.grant_group)
            ):
                batch_groups[plan.risk].append((planned_call.id, plan))

        for risk, planned_items in batch_groups.items():
            if not planned_items:
                continue
            label = "未知命令" if risk == "unknown" else "工作区写入命令"
            commands = "\n".join(
                f"{index}. {plan.command}"
                for index, (_call_id, plan) in enumerate(planned_items, start=1)
            )
            decision = {"approved": False, "mode": "once"}
            if permission_channel:
                decision = plan_permissions.request_and_wait(
                    channel=permission_channel,
                    tool="bash",
                    args={"command": commands},
                    reason=f"scope_bash_{risk}",
                    detail=(
                        f"本次计划包含 {len(planned_items)} 条{label}。"
                        "以下命令将按列表顺序执行：\n" + commands
                    ),
                    domain=planned_items[0][1].grant_group,
                    approval_choices="once,always",
                )
            if decision.get("mesh_pending"):
                termination = "mesh_yield"
                return _loop_return(reply="")
            if bool(decision.get("approved")):
                batch_allowed_calls.update(call_id for call_id, _plan in planned_items)
                if str(decision.get("mode") or "once") == "always":
                    grant_task_group(command_authorization or {}, planned_items[0][1].grant_group)
            else:
                batch_denied_calls.update(call_id for call_id, _plan in planned_items)

        for tc in tool_calls:
            if cancel_check:
                cancel_check()
            name = tc.function.name
            raw_args = tc.function.arguments or "{}"
            try:
                args = json.loads(raw_args) if raw_args else {}
            except json.JSONDecodeError:
                args = {}
            if last_failed_call is not None:
                append_audit(
                    "recovery",
                    prior_tool_call_id=last_failed_call["tool_call_id"],
                    prior_tool=last_failed_call["tool"],
                    prior_outcome=last_failed_call["outcome"],
                    next_tool_call_id=tc.id,
                    next_tool=name,
                )
                last_failed_call = None
            append_audit(
                "next_action",
                action="tool_call",
                tool_call_id=tc.id,
                tool=name,
            )
            append_audit(
                "tool_started",
                tool_call_id=tc.id,
                tool=name,
                args=args,
            )
            emit(make_trace_step("hook", "PreToolUse", name))
            hook_denial_reason = ""
            if user_hooks:
                from server.runtime.hooks import dispatch as dispatch_hooks

                hook_decision = dispatch_hooks(user_hooks, "PreToolUse", {**hook_payload, "tool": {"call_id": tc.id, "name": name, "args": args}})
                if hook_decision.decision == "deny":
                    hook_denial_reason = hook_decision.reason or "Denied by Hook policy."
            emit(make_trace_step("tool_call", name, preview_text(args)))
            absorb_goal = absorb_into_goal and task_intent != "independent"
            if goal_controller is not None and absorb_goal:
                goal_controller.before_tool(name, args, tc.id)

            from server.runtime import permissions as perm
            from server.runtime.tools import files as file_tools

            call_key = hashlib.sha256(
                (
                    name
                    + "\0"
                    + json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
                ).encode("utf-8")
            ).hexdigest()
            result: ToolResult | None = None
            live_process_query = name in {"bash_status", "bash_wait"}
            gate: dict[str, str] | None = None
            tool_kind, tool_sem = _resolve_tool_semantics(
                name, args if isinstance(args, dict) else {}
            )
            command_plan = command_plans.get(tc.id)
            budget_rid: str | None = None
            tool_executed = False
            if hook_denial_reason:
                result = ToolResult(
                    hook_denial_reason,
                    "denied",
                    {"tool_call_id": tc.id, "tool_name": name, "tool_kind": tool_kind, "hook_denied": True},
                )
            elif tc.id in batch_denied_calls:
                result = ToolResult(
                    "Permission denied by user for this command plan. Do not retry it without a new plan.",
                    "denied",
                    {
                        "tool_call_id": tc.id,
                        "tool_name": name,
                        "tool_kind": tool_kind,
                        "permission_denied": True,
                    },
                )
            elif call_key in readonly_cache:
                cached = readonly_cache[call_key]
                result = ToolResult(
                    cached.content
                    + "\n\n[RunLoopGuard] Query result served from cache. "
                    "Do not re-probe; submit acceptance if GoalCriteria are met.",
                    "ok",
                    {
                        **dict(cached.metadata or {}),
                        "tool_call_id": tc.id,
                        "tool_name": name,
                        "tool_kind": tool_kind,
                        "cached_readonly": True,
                        "loop_guard": False,
                    },
                )
                emit(make_trace_step("hook", "ReadonlyCacheHit", name))
            elif call_key in denied_calls:
                result = ToolResult(
                    "Repeated elevated action blocked (already denied). "
                    "Choose a different permitted approach — this is RunLoopGuard, "
                    "not a GoalCriteria failure.",
                    "denied",
                    {
                        "tool_call_id": tc.id,
                        "tool_name": name,
                        "tool_kind": tool_kind,
                        "loop_guard": True,
                    },
                )
                emit(make_trace_step("hook", "RepeatedCallBlocked", name))
            elif call_key in in_doubt_calls:
                result = ToolResult(
                    "该操作的结果尚未确认。请先核验已有资源，禁止重放同一写入操作。",
                    "in_doubt",
                    {
                        "tool_call_id": tc.id,
                        "tool_name": name,
                        "tool_kind": tool_kind,
                        "loop_guard": True,
                        "no_replay": True,
                    },
                )
                emit(make_trace_step("hook", "InDoubtWriteBlocked", name))
            elif not live_process_query and call_attempts.get(call_key, 0) >= 2:
                if tool_kind == "query":
                    msg = (
                        "Query frequency limit: identical probe blocked after two attempts. "
                        "Result may already be cached; do not retry reads to clear a gate. "
                        "If hard acceptance is met, stop and submit the final claim."
                    )
                else:
                    msg = (
                        "Repeated no-progress call blocked after two identical attempts. "
                        "Change arguments or strategy. This is RunLoopGuard, not Goal failure."
                    )
                result = ToolResult(
                    msg,
                    "error",
                    {
                        "tool_call_id": tc.id,
                        "tool_name": name,
                        "tool_kind": tool_kind,
                        "loop_guard": True,
                    },
                )
                emit(make_trace_step("hook", "RepeatedCallBlocked", name))

            from server.runtime.tool_semantics import dingtalk_cli_args

            connector_args = dingtalk_cli_args(
                name, args if isinstance(args, dict) else None
            )
            if (
                result is None
                and connector_args is not None
                and run_objective is not None
                and session_state is not None
            ):
                from server.connectors.dingtalk_operations import describe_dingtalk_command
                from server.runtime.attempt_ledger import AttemptLedger, AttemptState

                operation = describe_dingtalk_command(connector_args)
                if operation.effect == "read":
                    operation = None
                ledger_snapshot = AttemptLedger.from_context(session_state.context)
                already_in_doubt = any(
                    attempt.objective_id == run_objective.objective_id
                    and attempt.status == "in_doubt"
                    and operation is not None
                    and str((attempt.receipt or {}).get("operation_id") or "") == operation.operation_id
                    for attempt in (AttemptState.from_dict(raw) for raw in ledger_snapshot.attempts.values())
                )
                if already_in_doubt:
                    result = ToolResult(
                        "同一钉钉写入操作已有未确认结果。只能先用查询命令核验已有资源，不能再次创建、删除或更新。",
                        "in_doubt",
                        {
                            "tool_call_id": tc.id,
                            "tool_name": name,
                            "tool_kind": tool_kind,
                            "loop_guard": True,
                            "no_replay": True,
                        },
                    )
                    emit(make_trace_step("hook", "InDoubtWriteBlocked", name))

            if result is None and run_scope is not None:
                from server.runtime.scope_guard import classify_scope

                # Older in-process callers do not have a session-backed task
                # authorization context or a UI channel. Preserve their
                # durable-goal bash behavior while the app path uses the new
                # command-plan policy unconditionally.
                if (
                    command_authorization is None
                    and name == "bash"
                    and bool(getattr(run_scope, "allow_bash", False))
                ):
                    scope_gate = None
                else:
                    scope_gate = classify_scope(
                        run_scope,
                        name,
                        args if isinstance(args, dict) else {},
                        str(executor.cwd),
                        executor.trusted_roots,
                    )
                if scope_gate and scope_gate.get("reason") == "scope_explicit_request":
                    from server.runtime.tool_authorization import explicitly_requested

                    if explicitly_requested(
                        run_scope.user_request, name, args,
                        client=client, model=model, usage_context=usage_context,
                    ):
                        scope_gate = None
                if (
                    scope_gate
                    and scope_gate.get("action") == "ask"
                    and (
                        tc.id in batch_allowed_calls
                        or has_task_grant(
                            command_authorization or {},
                            str(scope_gate.get("domain") or ""),
                        )
                    )
                ):
                    scope_gate = None
                if scope_gate and scope_gate.get("action") == "deny":
                    result = ToolResult(
                        str(scope_gate.get("detail") or "scope denied"),
                        "denied",
                        {
                            "tool_call_id": tc.id,
                            "tool_name": name,
                            "scope_violation": True,
                            "tool_kind": tool_kind,
                            "goal_role": "control",
                        },
                    )
                    emit(
                        make_trace_step(
                            "hook",
                            "ScopeViolation",
                            str(scope_gate.get("detail") or ""),
                        )
                    )
                elif scope_gate and scope_gate.get("action") == "ask":
                    # Defer to the shared permission ask path below.
                    gate = {
                        "action": perm.ACTION_ASK,
                        "reason": str(scope_gate.get("reason") or "scope"),
                        "detail": str(scope_gate.get("detail") or ""),
                        "domain": str(scope_gate.get("domain") or ""),
                        "approval_choices": str(
                            scope_gate.get("approval_choices") or ""
                        ),
                    }

            if result is None and budget_guard:
                from server.runtime.budgets import check_hard, release, reserve

                if check_hard(run_budget, "tool_call") == "hard":
                    return _budget_exhausted(detail="tool_call hard limit")
                budget_rid = f"tool_{tc.id}_{uuid.uuid4().hex[:8]}"
                if not reserve(run_budget.root_budget_id, budget_rid, "tool_call", 1):
                    if budget_rid:
                        release(budget_rid)
                    return _budget_exhausted(detail="tool_call reserve failed")

            attempt_id: str | None = None
            ledger = None
            args_dict = args if isinstance(args, dict) else {}
            if (
                attempt_ledger_on
                and session_state is not None
                and run_objective is not None
                and result is None
            ):
                from server.runtime.attempt_ledger import AttemptLedger
                from server.runtime.objectives import (
                    canonical_send_payload_digest,
                    maybe_resolve_and_freeze,
                    message_text_from_tool_args,
                    operation_payload_digest_for_tool,
                    payload_digest,
                )

                # Resolve/freeze from send args before dispatch so digests match.
                goal_role_pre = str(
                    getattr(tool_sem, "goal_role", tool_kind) if tool_sem else tool_kind
                )
                if goal_role_pre == "terminal_action":
                    run_objective = maybe_resolve_and_freeze(
                        run_objective,
                        tool_name=name,
                        args=args_dict,
                        content="",
                        metadata={},
                        goal_role=goal_role_pre,
                    )

                ledger = AttemptLedger.from_context(session_state.context)
                ledger.objectives[run_objective.objective_id] = run_objective.to_dict()
                effect = str(getattr(tool_sem, "effect", "unknown") if tool_sem else "unknown")
                goal_role = goal_role_pre
                if goal_role_pre == "terminal_action":
                    display = str(
                        (getattr(run_objective, "target_ref", None) or {}).get(
                            "display_name"
                        )
                        or ""
                    )
                    if dingtalk_cli_args(name, args_dict) is not None:
                        attempted_digest = operation_payload_digest_for_tool(
                            tool_name=name,
                            args=args_dict,
                            fallback_payload=args_dict,
                        )
                    else:
                        _, attempted_digest = canonical_send_payload_digest(
                            message_text=message_text_from_tool_args(args_dict),
                            resolved_target_id=str(
                                getattr(run_objective, "resolved_target_id", "") or ""
                            )
                            or None,
                            target_display_name=display or None,
                        )
                else:
                    attempted_digest = (
                        payload_digest(args_dict) if args_dict else ""
                    )
                attempt = ledger.create_attempt(
                    objective_id=run_objective.objective_id,
                    tool_name=name,
                    effect=effect,
                    goal_role=goal_role,
                    resolved_target_id=str(
                        getattr(run_objective, "resolved_target_id", "") or ""
                    ),
                    attempted_payload_digest=attempted_digest,
                    objective_revision=int(getattr(run_objective, "revision", 1)),
                    idempotency_key=str(getattr(run_objective, "idempotency_key", "") or ""),
                )
                attempt_id = attempt.attempt_id
                session_state.context["execution_ledger"] = ledger.to_context()
                run_id = str(getattr(goal_controller, "run_id", "") or "")
                if run_id:
                    try:
                        from server.runtime.run_transcript import runs_root

                        ledger.append_event(
                            run_id,
                            {
                                "type": "attempt_created",
                                "attempt_id": attempt_id,
                                "tool_name": name,
                                "objective_id": run_objective.objective_id,
                            },
                            runs_root(),
                        )
                    except Exception:
                        pass

            def _dispatch_tool() -> ToolResult:
                nonlocal tool_executed
                if mesh_work:
                    allocation = mesh_budget.reserve_tool(mesh_work, name)
                    if not allocation["allowed"]:
                        return ToolResult("Task budget requires a user decision.", "waiting",
                                          {"mesh_task_id": mesh_work["task_id"]})
                if attempt_id and ledger is not None:
                    ledger.mark_dispatching(attempt_id)
                    session_state.context["execution_ledger"] = ledger.to_context()
                tool_executed = True
                if name == "bash" and command_plan is not None and command_plan.can_run_without_shell:
                    return file_tools.run_verified_readonly_commands(
                        executor.cwd,
                        command_plan.argv_groups,
                    )
                return executor.execute(
                    name,
                    args,
                    tool_call_id=tc.id,
                    scope_key=getattr(goal_controller, "board_scope", ""),
                )

            if result is None:
                gate = perm.combine_gates(
                    gate,
                    perm.classify_permission(
                        name, args, cwd=executor.cwd, policy=policy,
                        skill_roots=executor.skill_roots,
                        trusted_roots=executor.trusted_roots,
                        connector_executables=executor.connector_executables,
                    ),
                )
            if result is not None:
                pass
            elif gate:
                action = gate.get("action") or perm.ACTION_ASK
                emit(
                    make_trace_step(
                        "hook",
                        "PermissionRequest",
                        f"{gate['reason']}/{action}: {name}",
                    )
                )
                if action == perm.ACTION_DENY:
                    result = ToolResult(
                        f"Permission denied by policy ({gate['reason']}). "
                        f"{gate.get('detail', '')}",
                        "denied",
                        {
                            "tool_call_id": tc.id,
                            "tool_name": name,
                            "scope_key": getattr(goal_controller, "board_scope", ""),
                            "permission_denied": True,
                        },
                    )
                    emit(make_trace_step("hook", "PermissionDenied", name))
                elif action == perm.ACTION_ALLOW:
                    tok_ext = tok_bash = None
                    try:
                        if gate["reason"] == "external_path" or gate.get("allow_external"):
                            tok_ext = file_tools.allow_external(True)
                        if gate["reason"] == "dangerous_bash" or gate.get("allow_dangerous_bash"):
                            tok_bash = file_tools.allow_dangerous_bash(True)
                        if gate["reason"] == "scope_bash" and run_scope is not None:
                            run_scope.allow_bash = True
                        result = _dispatch_tool()
                        emit(make_trace_step("hook", "PermissionGranted", name))
                    finally:
                        if tok_ext is not None:
                            file_tools.reset_allow_external(tok_ext)
                        if tok_bash is not None:
                            file_tools.reset_allow_dangerous_bash(tok_bash)
                else:
                    # ask
                    decision = {"approved": False, "mode": "once", "domain": ""}
                    if permission_channel:
                        decision = perm.request_and_wait(
                            channel=permission_channel,
                            tool=name,
                            args=args if isinstance(args, dict) else {},
                            reason=gate["reason"],
                            detail=gate["detail"],
                            domain=str(gate.get("domain") or ""),
                            approval_choices=str(gate.get("approval_choices") or ""),
                        )
                    if decision.get("mesh_pending"):
                        termination = "mesh_yield"
                        return _loop_return(reply="")
                    approved = bool(decision.get("approved"))
                    grant_mode = str(decision.get("mode") or "once")
                    if gate.get("approval_choices") == "once":
                        grant_mode = "once"
                    if not approved:
                        result = ToolResult(
                            "Permission denied by user. "
                            "Do not retry the same elevated action without asking again.",
                            "denied",
                            {
                                "tool_call_id": tc.id,
                                "tool_name": name,
                                "scope_key": getattr(goal_controller, "board_scope", ""),
                                "permission_denied": True,
                                "scope_violation": gate.get("reason", "").startswith(
                                    "scope_"
                                ),
                            },
                        )
                        emit(make_trace_step("hook", "PermissionDenied", name))
                    else:
                        tok_ext = tok_bash = None
                        try:
                            if gate["reason"] == "external_path" or gate.get("allow_external"):
                                tok_ext = file_tools.allow_external(True)
                            if gate["reason"] == "dangerous_bash" or gate.get("allow_dangerous_bash"):
                                tok_bash = file_tools.allow_dangerous_bash(True)
                            if gate["reason"] == "scope_bash" and run_scope is not None:
                                # Bash ask historically expands for the run.
                                if grant_mode == "always":
                                    run_scope.allow_bash = True
                            if (
                                grant_mode == "always"
                                and run_scope is not None
                                and gate["reason"]
                                in {
                                    "scope_runtime_edit",
                                    "scope_runtime_domain",
                                }
                            ):
                                run_scope.allow_runtime_edit = True
                            if (
                                grant_mode == "always"
                                and run_scope is not None
                                and gate["reason"]
                                in {
                                    "scope_capability_domain",
                                    "scope_unknown_mcp",
                                    "scope_domain",
                                }
                            ):
                                domain = str(
                                    gate.get("domain")
                                    or decision.get("domain")
                                    or ""
                                )
                                if domain:
                                    run_scope.grant_domain(domain)
                            result = _dispatch_tool()
                            emit(make_trace_step("hook", "PermissionGranted", name))
                        finally:
                            if tok_ext is not None:
                                file_tools.reset_allow_external(tok_ext)
                            if tok_bash is not None:
                                file_tools.reset_allow_dangerous_bash(tok_bash)
            else:
                result = _dispatch_tool()
            if budget_rid:
                from server.runtime.budgets import commit, release

                if tool_executed:
                    commit(budget_rid, 1)
                else:
                    release(budget_rid)
            assert result is not None
            result = _merge_semantics_metadata(result, tool_sem)
            # Cache hits do not consume attempt budget or poison Goal failures.
            is_cache_hit = bool((result.metadata or {}).get("cached_readonly"))
            if not is_cache_hit:
                call_attempts[call_key] = call_attempts.get(call_key, 0) + 1
            # Only permanent-block true permission/user denials. Acceptance and
            # other recoverable tool denies must remain retryable after the
            # environment changes (e.g. plugin installed, then complete_task).
            if result.outcome == "denied" and bool(
                (result.metadata or {}).get("permission_denied")
            ):
                denied_calls.add(call_key)
            if result.outcome == "in_doubt" and bool((result.metadata or {}).get("no_replay")):
                in_doubt_calls.add(call_key)
            if (
                not is_cache_hit
                and not live_process_query
                and result.outcome == "ok"
                and (
                    name in READONLY_CACHE_TOOLS
                    or (result.metadata or {}).get("tool_kind") == "query"
                )
                and not (result.metadata or {}).get("loop_guard")
            ):
                readonly_cache[call_key] = result

            if cancel_check:
                cancel_check()
            if goal_controller is not None and absorb_goal:
                goal_controller.after_tool(result)

            if attempt_id and ledger is not None and session_state is not None:
                from server.runtime.attempt_ledger import AttemptState
                from server.runtime.objectives import (
                    canonical_send_payload_digest,
                    maybe_resolve_and_freeze,
                    message_text_from_tool_args,
                    operation_payload_digest_for_tool,
                    payload_digest,
                )

                receipt_meta = dict(result.metadata or {})
                acceptance_receipt = receipt_meta.get("acceptance_receipt")
                # Resolve/freeze objective from query/send evidence before acceptance.
                if run_objective is not None and result.outcome == "ok":
                    goal_role = str(
                        getattr(tool_sem, "goal_role", "") if tool_sem else ""
                    )
                    updated = maybe_resolve_and_freeze(
                        run_objective,
                        tool_name=name,
                        args=args_dict,
                        content=str(result.content or ""),
                        metadata=receipt_meta,
                        goal_role=goal_role,
                    )
                    if updated is not run_objective:
                        run_objective = updated
                        ledger.objectives[run_objective.objective_id] = (
                            run_objective.to_dict()
                        )
                if tool_executed:
                    if result.outcome == "ok":
                        ledger.mark_succeeded(
                            attempt_id,
                            receipt=receipt_meta.get("receipt") or receipt_meta or None,
                            acceptance_receipt=(
                                acceptance_receipt if isinstance(acceptance_receipt, dict) else None
                            ),
                        )
                        goal_role = str(
                            getattr(tool_sem, "goal_role", "") if tool_sem else ""
                        )
                        if (
                            goal_role == "terminal_action"
                            and dingtalk_cli_args(name, args_dict) is None
                        ):
                            display = str(
                                (getattr(run_objective, "target_ref", None) or {}).get(
                                    "display_name"
                                )
                                or ""
                            )
                            _, digest = canonical_send_payload_digest(
                                message_text=message_text_from_tool_args(args_dict),
                                resolved_target_id=str(
                                    getattr(run_objective, "resolved_target_id", "") or ""
                                )
                                or None,
                                target_display_name=display or None,
                            )
                        elif goal_role == "terminal_action":
                            digest = operation_payload_digest_for_tool(
                                tool_name=name,
                                args=args_dict,
                                fallback_payload=args_dict,
                            )
                        else:
                            digest = payload_digest(args_dict) if args_dict else ""
                        target_id = str(getattr(run_objective, "resolved_target_id", "") or "")
                        obj_revision = int(getattr(run_objective, "revision", 1))
                        obj_id = run_objective.objective_id
                        for old_raw in list(ledger.attempts.values()):
                            old = AttemptState.from_dict(old_raw)
                            if old.attempt_id == attempt_id or old.status != "failed":
                                continue
                            if (
                                old.objective_id == obj_id
                                and old.objective_revision == obj_revision
                                and old.resolved_target_id == target_id
                                and old.attempted_payload_digest == digest
                            ):
                                try:
                                    ledger.supersede(old.attempt_id, attempt_id)
                                except ValueError:
                                    pass
                    elif result.outcome == "in_doubt":
                        ledger.mark_in_doubt(
                            attempt_id,
                            receipt={
                                **receipt_meta,
                                "outcome": result.outcome,
                                "detail": preview_text(result.content, 300),
                            },
                        )
                    elif result.outcome != "waiting":
                        ledger.mark_failed(
                            attempt_id,
                            receipt={
                                "outcome": result.outcome,
                                "detail": preview_text(result.content, 300),
                            },
                        )
                elif result.outcome != "waiting":
                    ledger.mark_abandoned(
                        attempt_id,
                        receipt={
                            "outcome": result.outcome,
                            "detail": preview_text(result.content, 300),
                        },
                    )
                session_state.context["execution_ledger"] = ledger.to_context()
                run_id = str(getattr(goal_controller, "run_id", "") or "")
                if run_id:
                    try:
                        from server.runtime.run_transcript import runs_root

                        ledger.append_event(
                            run_id,
                            {
                                "type": "attempt_finished",
                                "attempt_id": attempt_id,
                                "outcome": result.outcome,
                            },
                            runs_root(),
                        )
                    except Exception:
                        pass

            trace.append(
                {
                    "tool": name,
                    "args": args,
                    "result": result.content[:2000],
                    "outcome": result.outcome,
                    "metadata": result.metadata,
                }
            )
            from server.agent_assets import recorder as asset_recorder
            asset_recorder.record_tool(
                assistant_id=str((usage_context or {}).get("agent_id") or ""),
                run_id=str((usage_context or {}).get("run_id") or ""), call_id=str(tc.id),
                name=name, args=args, result=result.content, outcome=result.outcome,
                metadata=result.metadata,
            )
            append_audit(
                "tool_finished",
                tool_call_id=tc.id,
                tool=name,
                outcome=result.outcome,
                result=result.content,
                metadata=result.metadata,
            )
            if result.outcome not in {"ok", "waiting"}:
                last_failed_call = {
                    "tool_call_id": tc.id,
                    "tool": name,
                    "outcome": result.outcome,
                }
            emit(make_trace_step("tool_result", name, preview_text(result.content)))
            emit(make_trace_step("hook", "PostToolUse", name))
            if user_hooks:
                from server.runtime.hooks import dispatch as dispatch_hooks

                dispatch_hooks(user_hooks, "PostToolUse", {**hook_payload, "tool": {"call_id": tc.id, "name": name, "args": args}, "tool_result": {"outcome": result.outcome, "result_summary": preview_text(result.content, 2000)}})
            api_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result.content,
                }
            )
            if (
                domain_acceptance_on
                and run_objective is not None
                and task_intent == "one_shot_action"
                and result.outcome == "ok"
            ):
                from server.runtime.acceptance import evaluate_acceptance
                from server.runtime.attempt_ledger import AttemptLedger, AttemptState

                acceptance_type = str((result.metadata or {}).get("acceptance_type") or "")
                if acceptance_type:
                    ledger = (
                        AttemptLedger.from_context(session_state.context)
                        if session_state is not None
                        else AttemptLedger()
                    )
                    attempts = [
                        AttemptState.from_dict(raw)
                        for raw in ledger.attempts.values()
                    ]
                    decision = evaluate_acceptance(
                        acceptance_type,
                        intent=task_intent,  # type: ignore[arg-type]
                        objective=run_objective,
                        attempts=attempts,
                    )
                    if decision.status == "accepted":
                        acceptance_decision = decision
                        receipt_label = ", ".join(decision.receipt_ids) or "ok"
                        final_text = (
                            f"完成：{decision.reason}（receipt: {receipt_label}）"
                        )
                        termination = "accepted"
                        stop_run = True
                        emit(
                            make_trace_step(
                                "hook",
                                "Accepted",
                                acceptance_type,
                            )
                        )
                        break
            if result.outcome == "ok" and result.metadata.get("mesh_yield"):
                termination = "mesh_yield"
                emit(make_trace_step("hook", "Stop", "task turn yielded; durable collaboration continues"))
                return _loop_return(reply="")
            if result.outcome == "waiting" and result.metadata.get("mesh_task_id"):
                termination = "mesh_yield"
                return _loop_return(reply="")
            if result.outcome == "waiting":
                # The resumed call must pair the original assistant tool_call with
                # the user's real answer, not this internal suspension marker.
                api_messages.pop()
                termination = "waiting_for_user"
                if goal_controller is not None:
                    goal_controller.mark_waiting_for_user(
                        str(result.metadata.get("question_id") or "")
                    )
                emit(
                    make_trace_step(
                        "hook",
                        "Stop",
                        "waiting_for_user",
                    )
                )
                return _loop_return(
                    reply="",
                    pending_question=result.metadata.get("pending_question"),
                )
        if stop_run:
            break
    else:
        termination = "max_turns"
        final_text = content or (
            f"[tool loop reached max rounds ({max_rounds}). "
            "Say「继续」to resume, or raise agent.max_turns in config.]"
        )
        log.warning("tool loop hit max_rounds=%s tools_used=%s", max_rounds, len(trace))
        emit(
            make_trace_step(
                "hook",
                "MaxRounds",
                f"reached max_rounds={max_rounds}; tools={len(trace)}",
            )
        )
        if goal_controller is not None:
            goal_controller.suspend_for_limit()

    emit(
        make_trace_step(
            "hook",
            "Stop",
            f"session used {len(trace)} tool calls",
        )
    )
    return _loop_return()


def _looks_like_plan_without_action(text: str) -> bool:
    """Heuristic: model narrated a plan / asked permission instead of calling tools.

    Requires stronger cues than a single common verb (e.g. bare「开始」) to avoid
    false ForceTools loops on ordinary replies.
    """
    t = (text or "").lower()
    strong = (
        "是否需要我",
        "要我现在",
        "可以开始吗",
        "shall i",
        "should i",
        "do you want me",
        "would you like me",
    )
    if any(m in t or m in (text or "") for m in strong):
        return True
    planish = sum(
        1
        for m in (
            "计划如下",
            "步骤：",
            "steps:",
            "plan:",
            "接下来我会",
            "我将按以下",
            "i will:",
            "i'll:",
        )
        if m in t or m in (text or "")
    )
    numbered = bool(re.search(r"(^|\n)\s*([1-9][\.\、]|[-*])\s+\S+", text or ""))
    return planish >= 1 and numbered
