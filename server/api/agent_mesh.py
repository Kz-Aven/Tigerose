"""Trusted local user management API; agent tools never use this router."""

from __future__ import annotations

import secrets
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from server.db import mesh_repos


def require_mesh_admin(request: Request) -> None:
    token = getattr(request.app.state, "mesh_admin_token", "")
    supplied = request.headers.get("authorization", "")
    if not token or not secrets.compare_digest(supplied.encode(), f"Bearer {token}".encode()):
        raise HTTPException(401, detail={"code": "permission_denied", "message": "Local management credential required"})


router = APIRouter(prefix="/api/agent-mesh", tags=["agent-mesh"], dependencies=[Depends(require_mesh_admin)])


class CancelBody(BaseModel):
    reason: str = Field(default="", max_length=4000)


class CollaborationBody(BaseModel):
    outgoing: list[str] = Field(default_factory=list)
    accepting_tasks: bool = True
    expected_revision: int = Field(ge=0)


class DecisionBody(BaseModel):
    action: str
    expected_task_revision: int = Field(ge=0)
    payload: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str = Field(min_length=1, max_length=200)


def _call(fn: Callable, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except KeyError as exc:
        raise HTTPException(404, detail={"code": "not_found", "message": "Mesh resource not found"}) from exc
    except PermissionError as exc:
        raise HTTPException(403, detail={"code": "permission_denied", "message": str(exc)}) from exc
    except ValueError as exc:
        code = getattr(exc, "code", "invalid_input")
        status = getattr(exc, "status", {"not_found": 404, "permission_denied": 403, "invalid_input": 400,
                                        "limit_reached": 429}.get(code, 409))
        raise HTTPException(status, detail={"code": code, "message": str(exc)}) from exc


@router.get("/tasks")
def list_tasks(assistant_id: str = "", role: str = "all", status: str = "", group_id: str = "",
               cursor: str = "", limit: int = Query(50, ge=1, le=100)):
    return _call(mesh_repos.list_tasks, actor_id=assistant_id or None, role=role, status=status,
                 group_id=group_id, cursor=cursor, limit=limit)


@router.get("/tasks/{task_id}")
def get_task(task_id: str):
    return _call(mesh_repos.get_task, task_id)


@router.get("/tasks/{task_id}/messages")
def get_messages(task_id: str, after_sequence: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=200)):
    items = _call(mesh_repos.messages, task_id, after_sequence=after_sequence)[:limit]
    return {"items": items, "next_sequence": items[-1]["sequence"] if items else after_sequence}


@router.get("/workflows/{workflow_id}/graph")
def get_graph(workflow_id: str):
    return _call(mesh_repos.graph, workflow_id)


@router.post("/tasks/{task_id}/cancel")
def cancel_task(task_id: str, body: CancelBody):
    return _call(mesh_repos.cancel_task, task_id, reason=body.reason)


@router.post("/workflows/{workflow_id}/cancel")
def cancel_workflow(workflow_id: str, body: CancelBody):
    return _call(mesh_repos.cancel_workflow, workflow_id, reason=body.reason)


@router.get("/assistants/{agent_id}/collaboration")
def collaboration(agent_id: str):
    return _call(mesh_repos.collaboration, agent_id)


@router.put("/assistants/{agent_id}/collaboration")
def configure(agent_id: str, body: CollaborationBody):
    return _call(mesh_repos.configure, agent_id, outgoing=body.outgoing,
                 accepting_tasks=body.accepting_tasks, expected_revision=body.expected_revision)


@router.get("/decisions/pending")
def pending_decisions():
    return _call(mesh_repos.pending_permission_decisions)


@router.post("/decisions/{decision_id}")
def decide(decision_id: str, body: DecisionBody):
    return _call(mesh_repos.decide, decision_id, action=body.action,
                 expected_task_revision=body.expected_task_revision,
                 payload=body.payload, idempotency_key=body.idempotency_key)


@router.get("/artifacts/{artifact_id}")
def get_artifact(artifact_id: str):
    artifact = _call(mesh_repos.artifact, artifact_id)
    return FileResponse(artifact["path"], filename=artifact["name"],
                        media_type=artifact.get("mime", "application/octet-stream"),
                        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
