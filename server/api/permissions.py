"""Human-in-the-loop permission resolve API."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from server.runtime import permissions as perm

router = APIRouter(prefix="/api/permissions", tags=["permissions"])


class PermissionResolve(BaseModel):
    approved: bool
    mode: str | None = None  # once | always


@router.get("")
def list_permissions(channel: str = Query(...)):
    if not (channel.startswith("group:") or channel.startswith("assistant:")):
        raise HTTPException(400, "channel must be group:{id} or assistant:{id}")
    return {"items": perm.list_pending(channel)}


@router.get("/{request_id}")
def get_permission(request_id: str):
    row = perm.get_pending(request_id)
    if not row:
        raise HTTPException(404, "permission request not found or already resolved")
    return row


@router.post("/{request_id}/resolve")
def resolve_permission(request_id: str, body: PermissionResolve):
    try:
        return perm.resolve(request_id, body.approved, mode=body.mode)
    except KeyError as e:
        raise HTTPException(404, "permission request not found or already resolved") from e
