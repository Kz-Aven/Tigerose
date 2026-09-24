"""Narrow command API for durable workflow state.

Clients may create tasks and submit the three reconciliation decisions, but
cannot directly overwrite execution status or receipts.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from server.db import workflow_repos
from server.runtime.feature_flags import flag_enabled

router = APIRouter(prefix="/api/workflows", tags=["workflows"])


class WorkflowCreate(BaseModel):
    session_id: str
    title: str = Field(min_length=1, max_length=500)
    kind: str = Field(default="user_workflow", min_length=1, max_length=80)
    intent: dict[str, Any] = Field(default_factory=dict)
    root_budget_id: str = ""


class TaskCreate(BaseModel):
    session_id: str
    title: str = Field(min_length=1, max_length=500)
    intent: dict[str, Any] = Field(default_factory=dict)
    executor_kind: str = Field(default="manual", max_length=100)
    acceptance_policy: str = Field(default="manual", max_length=100)
    priority: int = 0
    depends_on: list[tuple[str, str]] = Field(default_factory=list)
    parent_task_id: str = ""
    retry_policy: dict[str, Any] = Field(default_factory=dict)
    workspace_path: str = ""
    merge_target: str = ""


class DecisionBody(BaseModel):
    session_id: str
    decision: str


class RevisionBody(BaseModel):
    session_id: str
    intent: dict[str, Any] = Field(default_factory=dict)


class MergeDecisionBody(BaseModel):
    session_id: str
    approved: bool


def _require_v1() -> None:
    if not flag_enabled("durable_workflow_v1"):
        raise HTTPException(404, "durable workflow runtime is disabled")


def _scoped(workflow_id: str, session_id: str) -> dict[str, Any]:
    workflow = workflow_repos.get_workflow_for_scope(workflow_id, session_id)
    if workflow is None:
        raise HTTPException(404, "workflow not found")
    return workflow


def _reject_mesh(workflow_id: str) -> None:
    workflow = workflow_repos.get_workflow(workflow_id)
    if workflow and (str(workflow.get("kind", "")).startswith("mesh") or workflow.get("kind") == "agent_mesh"):
        raise HTTPException(409, "Mesh workflows require the agent-mesh domain API")


@router.post("")
def create_workflow(body: WorkflowCreate):
    _require_v1()
    if body.kind.startswith("mesh") or body.kind == "agent_mesh":
        raise HTTPException(409, "Mesh workflows require the agent-mesh domain API")
    return workflow_repos.create_workflow(
        kind=body.kind,
        owner_session_id=body.session_id,
        title=body.title,
        intent=body.intent,
        executor_kind="manual",
        acceptance_policy="manual",
        root_budget_id=body.root_budget_id,
    )


@router.get("")
def list_workflows(session_id: str = Query(...), limit: int = Query(10, ge=1, le=100)):
    _require_v1()
    workflows = workflow_repos.list_workflows(session_id, limit=limit)
    return {"items": [workflow_repos.workflow_projection(str(item["workflow_id"])) for item in workflows]}


def _projections_for_sessions(session_ids: list[str]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for session_id in session_ids:
        for workflow in workflow_repos.list_workflows(session_id, limit=20):
            workflow_id = str(workflow["workflow_id"])
            if workflow_id not in seen:
                seen.add(workflow_id)
                projection = workflow_repos.workflow_projection(workflow_id)
                if projection is not None:
                    out.append(projection)
    return out


@router.get("/assistant/{template_id}/current")
def list_current_assistant_workflows(template_id: str):
    _require_v1()
    from server.api import assistant_session_ops as sessions

    current = sessions.current_payload(template_id)
    return {"items": _projections_for_sessions([str(current["session_id"])] if current else [])}


@router.get("/group/{group_id}")
def list_group_workflows(group_id: str):
    _require_v1()
    from server.db import repos
    from server.runtime.turn import _get_sessions, scope_group_agent

    if not repos.get_group(group_id):
        raise HTTPException(404, "group not found")
    sessions = _get_sessions()
    session_ids = []
    for member in repos.list_members(group_id):
        session_id = sessions.resolve_session_id(scope_group_agent(group_id, str(member["instance_id"])))
        if session_id:
            session_ids.append(str(session_id))
    return {"items": _projections_for_sessions(session_ids)}


@router.get("/{workflow_id}")
def get_workflow(workflow_id: str, session_id: str = Query(...)):
    _require_v1()
    _scoped(workflow_id, session_id)
    return workflow_repos.workflow_projection(workflow_id)


@router.post("/{workflow_id}/tasks")
def create_task(workflow_id: str, body: TaskCreate):
    _require_v1()
    _scoped(workflow_id, body.session_id)
    _reject_mesh(workflow_id)
    try:
        return workflow_repos.create_task(
            workflow_id,
            title=body.title,
            intent=body.intent,
            executor_kind=body.executor_kind,
            acceptance_policy=body.acceptance_policy,
            priority=body.priority,
            depends_on=body.depends_on,
            parent_task_id=body.parent_task_id,
            retry_policy=body.retry_policy,
            workspace_path=body.workspace_path,
            merge_target=body.merge_target,
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/pending-actions/{action_id}/decision")
def decide_pending_action(action_id: str, body: DecisionBody, request: Request):
    _require_v1()
    from server.db.connection import get_connection

    conn = get_connection()
    try:
        action = conn.execute("SELECT workflow_id FROM pending_actions WHERE action_id=?", (action_id,)).fetchone()
    finally:
        conn.close()
    if action:
        _reject_mesh(str(action["workflow_id"]))
    actor = request.headers.get("x-avent-actor") or "local"
    try:
        decided = workflow_repos.decide_pending_action(
            action_id,
            owner_session_id=body.session_id,
            decision=body.decision,
            actor=actor,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not decided:
        raise HTTPException(409, "pending action is unavailable or already resolved")
    return {"ok": True}


@router.post("/tasks/{task_id}/revision")
def revise_task(task_id: str, body: RevisionBody, request: Request):
    _require_v1()
    task = workflow_repos.get_task(task_id)
    if task is None:
        raise HTTPException(404, "workflow task not found")
    _scoped(str(task["workflow_id"]), body.session_id)
    _reject_mesh(str(task["workflow_id"]))
    revised = workflow_repos.revise_task(
        task_id,
        intent=body.intent,
        actor=request.headers.get("x-avent-actor") or "local",
    )
    if revised is None:
        raise HTTPException(409, "workflow task cannot be revised")
    return revised


@router.post("/merge-gates/{gate_id}/decision")
def decide_merge_gate(gate_id: str, body: MergeDecisionBody, request: Request):
    _require_v1()
    gate = workflow_repos.get_merge_gate(gate_id)
    if gate:
        _reject_mesh(str(gate["workflow_id"]))
    if not workflow_repos.decide_merge_gate(
        gate_id,
        owner_session_id=body.session_id,
        approved=body.approved,
        actor=request.headers.get("x-avent-actor") or "local",
    ):
        raise HTTPException(409, "merge gate is unavailable or already resolved")
    return {"ok": True}
