"""Persistent AskUserQuestion tool support."""

from __future__ import annotations

import json
from typing import Any

from server.db import repos
from server.runtime.executor import ToolResult


def validate_questions(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or not 1 <= len(raw) <= 4:
        raise ValueError("questions must contain 1-4 items")
    normalized: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, value in enumerate(raw):
        if not isinstance(value, dict):
            raise ValueError(f"questions[{index}] must be an object")
        question_id = str(value.get("id") or "").strip()
        header = str(value.get("header") or "").strip()
        question = str(value.get("question") or "").strip()
        options = value.get("options")
        if not question_id or question_id in seen_ids:
            raise ValueError(f"questions[{index}].id must be non-empty and unique")
        if not header or not question:
            raise ValueError(f"questions[{index}] requires header and question")
        if not isinstance(options, list) or not 2 <= len(options) <= 4:
            raise ValueError(f"questions[{index}].options must contain 2-4 items")
        normalized_options: list[dict[str, str]] = []
        labels: set[str] = set()
        for option_index, option in enumerate(options):
            if not isinstance(option, dict):
                raise ValueError(
                    f"questions[{index}].options[{option_index}] must be an object"
                )
            label = str(option.get("label") or "").strip()
            description = str(option.get("description") or "").strip()
            if not label or label in labels:
                raise ValueError(
                    f"questions[{index}].options labels must be non-empty and unique"
                )
            labels.add(label)
            normalized_options.append(
                {"label": label, "description": description}
            )
        seen_ids.add(question_id)
        normalized.append(
            {
                "id": question_id,
                "header": header,
                "question": question,
                "options": normalized_options,
                "multi_select": bool(value.get("multi_select", False)),
            }
        )
    return normalized


def ask_user_question(ctx, questions: Any, *, tool_call_id: str) -> ToolResult:
    payload = {"questions": validate_questions(questions)}
    if getattr(ctx, "mesh_task_id", ""):
        from server.runtime.mesh_runtime import ask_user

        decision = ask_user(ctx, payload["questions"])
        return ToolResult(json.dumps(decision, ensure_ascii=False), "waiting",
                          {"mesh_task_id": ctx.mesh_task_id})
    continuation = (
        ctx.session_state.context.get("pending_question_continuation")
        if ctx.session_state is not None and isinstance(ctx.session_state.context, dict)
        else None
    )
    prior_run_ids = (
        continuation.get("usage_run_ids")
        if isinstance(continuation, dict)
        else []
    )
    usage_run_ids = (
        [str(run_id).strip() for run_id in prior_run_ids if str(run_id).strip()]
        if isinstance(prior_run_ids, list)
        else []
    )
    if ctx.run_id:
        usage_run_ids.append(ctx.run_id)
    payload["usage_run_ids"] = list(dict.fromkeys(usage_run_ids))
    goal = (
        ctx.session_state.context.get("goal")
        if ctx.session_state is not None and isinstance(ctx.session_state.context, dict)
        else None
    )
    item = repos.create_pending_question(
        tool_call_id=tool_call_id,
        goal_id=str(goal.get("goal_id") or "") if isinstance(goal, dict) else "",
        run_id=ctx.run_id,
        session_id=ctx.session_id,
        channel=ctx.sse_channel,
        surface=ctx.surface,
        template_id=ctx.template_id,
        group_id=ctx.group_id or "",
        instance_id=ctx.instance_id or "",
        payload=payload,
    )
    return ToolResult(
        "Waiting for the user's answer.",
        "waiting",
        {
            "question_id": item["question_id"],
            "pending_question": item,
        },
    )


def format_answer_tool_result(item: dict) -> str:
    return json.dumps(
        {
            "question_id": item["question_id"],
            "answers": (item.get("answer") or {}).get("answers") or [],
        },
        ensure_ascii=False,
    )
