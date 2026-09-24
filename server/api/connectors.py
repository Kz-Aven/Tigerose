"""DingTalk CLI connector APIs."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from server.connectors import manager
from server.db import repos

router = APIRouter(prefix="/api/connectors", tags=["connectors"])


class ConnectorEnabled(BaseModel):
    enabled: bool


def _set_enabled(template_id: str, connector_id: str, enabled: bool):
    template = repos.get_template(template_id)
    if not template:
        raise HTTPException(404, "assistant not found")
    if enabled and not manager.status(connector_id).authenticated:
        raise HTTPException(409, f"{manager.status(connector_id).display_name}未连接，请先完成授权")
    meta = dict(template.get("config_meta") or {})
    connectors = dict(meta.get("connectors") or {})
    connectors[connector_id] = {"enabled": bool(enabled)}
    meta["connectors"] = connectors
    return repos.update_template(template_id, config_meta=meta)


def _legacy_dingtalk_status():
    """Keep the released macOS client working while it migrates to generic status."""
    status = manager.status("dingtalk")
    operation = status.message if status.health == "degraded" else ""
    state = operation or (
        "connected"
        if status.authenticated
        else "auth_required"
        if status.installed
        else "not_installed"
    )
    return {
        "installed": status.installed,
        "connected": status.authenticated,
        "state": state,
        "account_name": status.account_name or None,
        "corp_name": status.corp_name or None,
        "message": status.message or None,
    }


def _disable_connector_everywhere(connector_id: str) -> None:
    for template in repos.list_templates():
        meta = dict(template.get("config_meta") or {})
        connectors = dict(meta.get("connectors") or {})
        value = connectors.get(connector_id)
        if value is True or (isinstance(value, dict) and value.get("enabled")):
            connectors[connector_id] = {"enabled": False}
            meta["connectors"] = connectors
            repos.update_template(template["template_id"], config_meta=meta)


@router.get("")
def list_connectors():
    return [item.as_dict() for item in manager.list_statuses()]


@router.post("/{connector_id}/install")
def install_connector(connector_id: str):
    try:
        return manager.install(connector_id).as_dict()
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/{connector_id}/connect")
def connect_connector(connector_id: str):
    try:
        return manager.connect(connector_id).as_dict()
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/lark/reconnect")
def reconnect_lark_connector():
    return manager.reconnect("lark").as_dict()


@router.post("/{connector_id}/disconnect")
def disconnect_connector(connector_id: str):
    try:
        result = manager.disconnect(connector_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    _disable_connector_everywhere(connector_id)
    return result.as_dict()


@router.post("/{connector_id}/uninstall")
def uninstall_connector(connector_id: str):
    try:
        result = manager.uninstall(connector_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    _disable_connector_everywhere(connector_id)
    return result.as_dict()


@router.put("/assistants/{template_id}/{connector_id}")
def set_assistant_connector(template_id: str, connector_id: str, body: ConnectorEnabled):
    return _set_enabled(template_id, connector_id, body.enabled)


# One release of API compatibility for the existing macOS client.
@router.get("/dingtalk")
def get_dingtalk():
    return _legacy_dingtalk_status()


@router.post("/dingtalk/connect")
def connect_dingtalk():
    manager.connect("dingtalk")
    return _legacy_dingtalk_status()


@router.post("/dingtalk/disconnect")
def disconnect_dingtalk():
    disconnect_connector("dingtalk")
    return _legacy_dingtalk_status()


@router.put("/assistants/{template_id}/dingtalk")
def set_assistant_dingtalk(template_id: str, body: ConnectorEnabled):
    return _set_enabled(template_id, "dingtalk", body.enabled)


@router.get("/{connector_id}")
def get_connector(connector_id: str):
    try:
        return manager.status(connector_id).as_dict()
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
