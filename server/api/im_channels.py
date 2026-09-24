"""Local configuration API for IM bot channels."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel

from server.db import repos
from server.im_channels import manager


router = APIRouter(prefix="/api/im-channels", tags=["im-channels"])


class BotBinding(BaseModel):
    template_id: str | None = None


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


@router.get("")
def list_channels(response: Response, platform: str | None = None):
    _no_store(response)
    try:
        return manager.list_channels(platform)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/{platform}/registrations", status_code=202)
def start_registration(platform: str, response: Response):
    _no_store(response)
    try:
        return manager.start_registration(platform)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/registrations/{registration_id}")
def get_registration(registration_id: str, response: Response):
    _no_store(response)
    registration = manager.registration(registration_id)
    if not registration:
        raise HTTPException(404, "registration not found")
    return registration


@router.post("/registrations/{registration_id}/refresh", status_code=202)
def refresh_registration(registration_id: str, response: Response):
    _no_store(response)
    registration = manager.refresh_registration(registration_id)
    if not registration:
        raise HTTPException(404, "registration not found")
    return registration


@router.delete("/registrations/{registration_id}")
def cancel_registration(registration_id: str, response: Response):
    _no_store(response)
    if not manager.cancel_registration(registration_id):
        raise HTTPException(404, "registration not found")
    return {"ok": True}


@router.put("/bots/{bot_id}/binding")
def bind_bot(bot_id: str, body: BotBinding):
    try:
        return manager.bind_bot(bot_id, body.template_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.delete("/bots/{bot_id}/binding")
def unbind_bot(bot_id: str):
    try:
        return manager.bind_bot(bot_id, None)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.delete("/bots/{bot_id}")
def delete_bot(bot_id: str):
    if not manager.delete_bot(bot_id):
        raise HTTPException(404, "bot not found")
    return {"ok": True}


@router.get("/assistants/{template_id}")
def assistant_bots(template_id: str):
    if not repos.get_template(template_id):
        raise HTTPException(404, "assistant not found")
    return manager.list_assistant_bots(template_id)
