"""Build bounded, replayable Context Packs for workflow attempts."""

from __future__ import annotations

import json
from typing import Any


def build_context_pack(task_id: str) -> dict[str, Any]:
    """Persist a compact task context without copying the full chat transcript."""
    from server.db import workflow_repos

    task = workflow_repos.get_task(task_id)
    if task is None:
        raise KeyError(f"unknown workflow task: {task_id}")
    workflow = workflow_repos.get_workflow(str(task["workflow_id"]))
    if workflow is None:
        raise KeyError(f"unknown workflow: {task['workflow_id']}")
    attempts = workflow_repos.list_attempts(task_id)
    events = workflow_repos.list_events(str(task["workflow_id"]))
    source_event_seq = int(events[-1]["sequence"]) if events else 0
    intent = dict(task.get("intent") or {})
    open_attempts = [
        {
            "attempt_id": attempt["attempt_id"],
            "status": attempt["status"],
            "idempotency_key": attempt["idempotency_key"],
            "error_code": attempt.get("error_code") or "",
        }
        for attempt in attempts
        if attempt.get("status") not in {"succeeded", "failed", "reconciled_succeeded", "reconciled_absent", "abandoned"}
    ]
    payload: dict[str, Any] = {
        "workflow_id": workflow["workflow_id"],
        "workflow_revision": workflow["revision"],
        "task_id": task_id,
        "task_revision": task["revision"],
        "title": task["title"],
        "intent": intent,
        "acceptance_policy": task["acceptance_policy"],
        "allowed_tools": list(intent.get("allowed_tools") or []),
        "workspace_cwd": str(intent.get("workspace_cwd") or ""),
        "root_budget_id": workflow.get("root_budget_id") or "",
        "open_attempts": open_attempts,
        "artifact_refs": workflow_repos.list_artifacts(task_id),
        "source_event_seq": source_event_seq,
    }
    summary = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    stored = workflow_repos.save_context_pack(
        task_id,
        revision=int(task["revision"]),
        summary=summary,
        open_items=open_attempts,
        artifact_refs=payload["artifact_refs"],
        source_event_seq=source_event_seq,
    )
    payload["pack_id"] = stored["pack_id"]
    return payload
