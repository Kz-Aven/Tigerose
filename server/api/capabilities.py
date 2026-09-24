"""Capability catalog and assistant binding APIs."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from server.capabilities.catalog import (
    RUNTIME_REQUIRED_TOOLS,
    annotate_management_flags,
    apply_registry,
    delete_capability_files,
    import_plugin_from_path,
    import_skill_from_path,
    normalize_capabilities,
    resolve_bundle,
    scan_all,
)
from server.capabilities.mcp_config import (
    delete_user_mcp_server,
    ensure_user_mcp_seeded,
    load_merged_mcp_servers,
    upsert_user_mcp_server,
)
from server.db import repos
from server.runtime.mcp import test_mcp_server

router = APIRouter(tags=["capabilities"])


class CapabilitiesUpdate(BaseModel):
    skills: list[str] = Field(default_factory=list)
    plugins: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    mcp_servers: list[str] = Field(default_factory=list)


class RegistryPatch(BaseModel):
    kind: str | None = None
    source_path: str | None = None
    display_name: str | None = None
    description: str | None = None
    meta: dict | None = None
    hidden: bool | None = None


class ImportBody(BaseModel):
    kind: str  # skill | plugin
    path: str


class McpServerBody(BaseModel):
    name: str
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    description: str = ""
    type: str | None = None
    url: str | None = None


class McpTestBody(BaseModel):
    workspace: str | None = None
    command: str | None = None
    args: list[str] | None = None
    env: dict[str, str] | None = None
    url: str | None = None


def _canonical_mcp_id(raw: str) -> str:
    """Return the catalog ID for a configured MCP server or its alias."""
    name = raw.removeprefix("mcp:").strip()
    if not name:
        return raw
    for configured_name, config in load_merged_mcp_servers().items():
        server_id = str(config.get("server_id") or configured_name).strip()
        aliases = {configured_name, server_id}
        aliases.update(str(alias).strip() for alias in config.get("aliases") or [])
        if name in aliases:
            return f"mcp:{server_id}"
    return raw if raw.startswith("mcp:") else f"mcp:{name}"


def _visible_catalog(kind: str | None = None, workspace: str | None = None) -> list[dict]:
    ensure_user_mcp_seeded()
    scanned = scan_all(workspace=workspace)
    registry = repos.list_capability_registry()
    catalog = annotate_management_flags(apply_registry(scanned, registry))
    if kind:
        catalog = [c for c in catalog if c.get("kind") == kind]
    return catalog


@router.get("/api/capabilities")
def list_capabilities(
    kind: str | None = Query(default=None),
    workspace: str | None = Query(default=None),
):
    if kind and kind not in ("skill", "plugin", "tool", "mcp"):
        raise HTTPException(400, "kind must be skill|plugin|tool|mcp")
    return _visible_catalog(kind, workspace)


@router.post("/api/capabilities/import")
def import_capability(body: ImportBody):
    kind = body.kind.strip()
    if kind not in ("skill", "plugin"):
        raise HTTPException(400, "kind must be skill|plugin")
    path = Path(body.path).expanduser()
    if not path.exists():
        raise HTTPException(404, f"path not found: {body.path}")
    try:
        if kind == "skill":
            item = import_skill_from_path(path)
        else:
            item = import_plugin_from_path(path)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return annotate_management_flags([item])[0]


@router.delete("/api/capabilities/{capability_id:path}")
def delete_capability(capability_id: str):
    catalog = _visible_catalog()
    item = next((c for c in catalog if c["id"] == capability_id), None)
    if not item:
        # Still allow tombstone / strip by id
        kind = capability_id.split(":", 1)[0] if ":" in capability_id else ""
        if kind == "tool":
            name = capability_id.removeprefix("tool:")
            if name in RUNTIME_REQUIRED_TOOLS:
                raise HTTPException(400, "runtime required tool cannot be deleted")
            raise HTTPException(400, "tools cannot be deleted from catalog")
        repos.upsert_capability_registry(
            capability_id,
            kind=kind or "skill",
            hidden=True,
        )
        repos.strip_capability_from_all_templates(capability_id)
        return {"ok": True, "mode": "tombstone"}

    kind = item.get("kind")
    source = item.get("source") or "builtin"
    if kind == "tool":
        raise HTTPException(400, "tools cannot be deleted from catalog")
    if not item.get("deletable"):
        raise HTTPException(400, "this capability is not deletable from Settings")

    if kind == "mcp":
        name = item["name"]
        if source == "project":
            raise HTTPException(400, "项目级 MCP 请在工作区 .agent/mcp.json 中修改")
        delete_user_mcp_server(name)
        # If it was only from seed/builtin overlay, also hide
        if name in load_merged_mcp_servers():
            # still present via seed — tombstone
            repos.upsert_capability_registry(capability_id, kind="mcp", hidden=True)
        repos.strip_capability_from_all_templates(capability_id)
        return {"ok": True, "mode": "mcp_delete"}

    if source == "user":
        try:
            delete_capability_files(item)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        repos.strip_capability_from_all_templates(capability_id)
        return {"ok": True, "mode": "file_delete"}

    if source == "builtin":
        repos.upsert_capability_registry(
            capability_id,
            kind=str(kind),
            source_path=str(item.get("source_path") or ""),
            display_name=str(item.get("display_name") or ""),
            description=str(item.get("description") or ""),
            hidden=True,
        )
        repos.strip_capability_from_all_templates(capability_id)
        return {"ok": True, "mode": "tombstone"}

    raise HTTPException(400, "cannot delete project-level capability from Settings")


@router.get("/api/assistants/{template_id}/capabilities")
def get_assistant_capabilities(
    template_id: str,
    workspace: str | None = Query(default=None),
):
    tpl = repos.get_template(template_id)
    if not tpl:
        raise HTTPException(404, "assistant not found")
    catalog = _visible_catalog(workspace=workspace)
    bundle = resolve_bundle(tpl.get("capabilities") or {}, catalog)
    return {
        "template_id": template_id,
        "capabilities": bundle["raw"],
        "resolved": {
            "skills": bundle["skills"],
            "plugins": bundle["plugins"],
            "tools": bundle["tools"],
            "mcp_servers": bundle["mcp_servers"],
        },
    }


@router.put("/api/assistants/{template_id}/capabilities")
def put_assistant_capabilities(template_id: str, body: CapabilitiesUpdate):
    template = repos.get_template(template_id)
    if not template:
        raise HTTPException(404, "assistant not found")
    caps = normalize_capabilities(body.model_dump())
    caps["mcp_servers"] = [_canonical_mcp_id(raw) for raw in caps["mcp_servers"]]
    persisted_mcp_ids = set(
        normalize_capabilities(template.get("capabilities") or {})["mcp_servers"]
    )
    catalog_ids = {c["id"] for c in _visible_catalog()}
    tool_names = {c["name"] for c in _visible_catalog("tool")}
    errors: dict[str, list[str]] = {}

    def check(kind_key: str, ids: list[str], *, tools: bool = False) -> list[str]:
        bad = []
        cleaned = []
        for raw in ids:
            if tools:
                name = raw.removeprefix("tool:")
                cid = raw if raw.startswith("tool:") else f"tool:{name}"
                if name not in tool_names and cid not in catalog_ids:
                    bad.append(raw)
                else:
                    cleaned.append(name)
            else:
                if raw not in catalog_ids:
                    bad.append(raw)
                else:
                    cleaned.append(raw)
        if bad:
            errors[kind_key] = bad
        return cleaned

    caps["skills"] = check("skills", caps["skills"])
    caps["plugins"] = check("plugins", caps["plugins"])
    caps["tools"] = check("tools", caps["tools"], tools=True)
    # A removed MCP must remain removable from the UI, but should not prevent
    # unrelated capability changes from being saved.
    caps["mcp_servers"] = check(
        "mcp_servers",
        [raw for raw in caps["mcp_servers"] if raw not in persisted_mcp_ids],
    ) + [raw for raw in caps["mcp_servers"] if raw in persisted_mcp_ids]
    if errors:
        raise HTTPException(400, detail={"message": "unknown capability ids", "fields": errors})

    tpl = repos.update_template(template_id, capabilities=caps)
    bundle = resolve_bundle(caps, _visible_catalog())
    return {
        "template_id": template_id,
        "capabilities": caps,
        "resolved": {
            "skills": bundle["skills"],
            "plugins": bundle["plugins"],
            "tools": bundle["tools"],
            "mcp_servers": bundle["mcp_servers"],
        },
        "template": tpl,
    }


@router.patch("/api/capability-registry/{capability_id:path}")
def patch_registry(capability_id: str, body: RegistryPatch):
    existing = None
    for row in repos.list_capability_registry():
        if row["capability_id"] == capability_id:
            existing = row
            break
    kind = body.kind or (existing or {}).get("kind")
    if not kind:
        kind = capability_id.split(":", 1)[0] if ":" in capability_id else "mcp"
    if kind not in ("skill", "plugin", "tool", "mcp"):
        raise HTTPException(400, "kind must be skill|plugin|tool|mcp")
    return repos.upsert_capability_registry(
        capability_id,
        kind=kind,
        source_path=body.source_path
        if body.source_path is not None
        else (existing or {}).get("source_path", ""),
        display_name=body.display_name
        if body.display_name is not None
        else (existing or {}).get("display_name", ""),
        description=body.description
        if body.description is not None
        else (existing or {}).get("description", ""),
        meta=body.meta if body.meta is not None else (existing or {}).get("meta"),
        hidden=body.hidden if body.hidden is not None else bool((existing or {}).get("hidden")),
    )


@router.get("/api/mcp-servers")
def list_mcp_servers(workspace: str | None = Query(default=None)):
    ensure_user_mcp_seeded()
    from server.capabilities.mcp_config import load_canonical_mcp_servers

    servers = load_canonical_mcp_servers(workspace)
    return [
        {
            "id": f"mcp:{server_id}",
            "name": server_id,
            "type": cfg.get("type"),
            "command": cfg.get("command"),
            "args": cfg.get("args") or [],
            "env": cfg.get("env") or {},
            "url": cfg.get("url"),
            "description": cfg.get("description") or "",
            "source": cfg.get("source") or "user",
            "editable": (cfg.get("source") or "user") == "user",
            "deletable": (cfg.get("source") or "user") in ("user", "builtin"),
        }
        for server_id, cfg in sorted(servers.items())
    ]


@router.put("/api/mcp-servers/{name}")
def put_mcp_server(name: str, body: McpServerBody):
    if body.name and body.name != name:
        raise HTTPException(400, "name mismatch")
    if not body.command and not body.url:
        raise HTTPException(400, "MCP server requires a command (stdio) or url (streamable-http)")
    # Clear tombstone if re-adding
    repos.upsert_capability_registry(f"mcp:{name}", kind="mcp", hidden=False)
    cfg: dict[str, Any] = {
        "args": body.args,
        "env": body.env,
        "description": body.description,
    }
    if body.command:
        cfg["command"] = body.command
    if body.url:
        cfg["url"] = body.url
        cfg["type"] = body.type or "streamable-http"
    entry = upsert_user_mcp_server(name, cfg)
    return entry


@router.delete("/api/mcp-servers/{name}")
def remove_mcp_server(name: str):
    ok = delete_user_mcp_server(name)
    repos.upsert_capability_registry(f"mcp:{name}", kind="mcp", hidden=True)
    repos.strip_capability_from_all_templates(f"mcp:{name}")
    if not ok:
        # still tombstoned seed
        return {"ok": True, "mode": "tombstone"}
    return {"ok": True, "mode": "deleted"}


@router.post("/api/mcp-servers/{name}/test")
def mcp_test(name: str, body: McpTestBody | None = None):
    body = body or McpTestBody()
    cfg = None
    if body.command:
        cfg = {
            "command": body.command,
            "args": body.args or [],
            "env": body.env or {},
        }
    elif body.url:
        cfg = {
            "url": body.url,
            "args": body.args or [],
            "env": body.env or {},
        }
    try:
        return test_mcp_server(name, workspace=body.workspace, cfg=cfg)
    except Exception as e:
        return {"ok": False, "error": str(e), "tools": []}
