"""Local management endpoints for user Hook configuration."""

from __future__ import annotations

import secrets
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from server.capabilities import hooks_config
from server.runtime.hooks import execute_handler


def require_hooks_admin(request: Request) -> None:
    token = getattr(request.app.state, "hooks_admin_token", "")
    supplied = request.headers.get("authorization", "")
    if not token or not secrets.compare_digest(supplied.encode(), f"Bearer {token}".encode()):
        raise HTTPException(401, detail={"code": "permission_denied", "message": "Local management credential required"})
    origin = request.headers.get("origin", "")
    if origin and not origin.startswith("http://127.0.0.1"):
        raise HTTPException(403, detail={"code": "untrusted_origin", "message": "Tigerose desktop origin required"})


router = APIRouter(prefix="/api/hooks", tags=["hooks"])
admin = [Depends(require_hooks_admin)]


class HookBody(BaseModel):
    hook_id: str | None = None
    display_name: str = ""
    enabled: bool = True
    event: str
    priority: int = 100
    matcher: dict[str, Any] = Field(default_factory=dict)
    handler: dict[str, Any]
    failure_policy: str = "allow"
    description: str = ""


class TestBody(BaseModel):
    hook: HookBody
    payload: dict[str, Any] = Field(default_factory=dict)


def _translate(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("")
def list_hooks(workspace: str | None = Query(default=None)):
    return hooks_config.resolved_hooks(workspace)


@router.get("/{hook_id}")
def read_hook(hook_id: str, scope: str = Query("user"), workspace: str | None = Query(default=None)):
    item = _translate(hooks_config.get_hook, hook_id, scope, workspace)
    if not item:
        raise HTTPException(404, "hook not found")
    return item


@router.put("/{hook_id}", dependencies=admin)
def put_hook(hook_id: str, body: HookBody, scope: str = Query("user"), workspace: str | None = Query(default=None)):
    raw = body.model_dump()
    if raw.get("hook_id") and raw["hook_id"] != hook_id:
        raise HTTPException(400, "hook_id must match path")
    return _translate(hooks_config.put_hook, hook_id, raw, scope, workspace)


@router.delete("/{hook_id}", dependencies=admin)
def delete_hook(hook_id: str, scope: str = Query("user"), workspace: str | None = Query(default=None)):
    if not _translate(hooks_config.delete_hook, hook_id, scope, workspace):
        raise HTTPException(404, "hook not found")
    return {"ok": True}


@router.post("/test", dependencies=admin)
def test_hook(body: TestBody):
    hook = _translate(hooks_config.validate_hook, body.hook.model_dump(), hook_id=body.hook.hook_id)
    return execute_handler(hook["handler"], {"schema_version": 1, **body.payload, "event": body.payload.get("event") or hook["event"]})
