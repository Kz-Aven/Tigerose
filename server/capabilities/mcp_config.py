"""MCP server config: project > user > builtin seed."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

from avent_paths import (
    code_root,
    ensure_data_dirs,
    project_agent_dir,
    user_mcp_config_path,
)

_SERVER_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

# Display-name → stable server_id. Used when migrating non-ASCII config keys.
KNOWN_SERVER_ID_MIGRATIONS: dict[str, str] = {
    "机器人消息": "dingtalk_robot_message",
    "钉钉机器人消息": "dingtalk_robot_message",
    "钉钉日历": "dingtalk_calendar",
}

DEFAULT_MCP_SEED: dict[str, dict[str, Any]] = {
    "codegraph": {
        "command": "codegraph",
        "args": ["serve", "--mcp", "--path", "${workspaceFolder}"],
        "env": {},
        "description": "Code intelligence over indexed knowledge graph",
        "server_id": "codegraph",
        "display_name": "codegraph",
    }
}


def is_valid_server_id(server_id: str) -> bool:
    return bool(server_id) and bool(_SERVER_ID_RE.match(server_id))


def allocate_server_id(display_name: str, existing: set[str]) -> str:
    """Create a stable [a-z0-9_-] server_id from a display name."""
    mapped = KNOWN_SERVER_ID_MIGRATIONS.get(display_name.strip())
    if mapped and mapped not in existing:
        return mapped
    if mapped and mapped in existing:
        n = 2
        while f"{mapped}_{n}" in existing:
            n += 1
        return f"{mapped}_{n}"
    if is_valid_server_id(display_name) and display_name not in existing:
        return display_name
    slug = re.sub(r"[^a-z0-9_-]+", "_", display_name.lower()).strip("_")
    if not slug or not is_valid_server_id(slug):
        import hashlib

        digest = hashlib.sha256(display_name.encode("utf-8")).hexdigest()[:10]
        slug = f"mcp_{digest}"
    base = slug
    n = 2
    while slug in existing:
        slug = f"{base}_{n}"
        n += 1
    return slug


def _normalize_entry(name: str, cfg: dict[str, Any]) -> dict[str, Any] | None:
    """Normalize one MCP server entry (stdio command or streamable-http url).

    Returns None for entries without a usable `command` or `url`.
    """
    cmd = cfg.get("command")
    url = cfg.get("url")
    if not cmd and not url:
        return None
    display = str(cfg.get("display_name") or name)
    server_id = str(cfg.get("server_id") or "").strip()
    if not server_id:
        server_id = name if is_valid_server_id(name) else ""
    entry: dict[str, Any] = {
        "type": str(cfg.get("type") or ("stdio" if cmd else "streamable-http")),
        "args": [str(a) for a in (cfg.get("args") or [])],
        "env": {str(k): str(v) for k, v in (cfg.get("env") or {}).items()}
        if isinstance(cfg.get("env"), dict)
        else {},
        "description": str(cfg.get("description") or ""),
        "display_name": display,
    }
    if server_id:
        entry["server_id"] = server_id
    aliases = cfg.get("aliases")
    if isinstance(aliases, list):
        entry["aliases"] = [str(a) for a in aliases]
    if cmd:
        entry["command"] = str(cmd)
    if url:
        entry["url"] = str(url)
    return entry


def migrate_server_keys(
    servers: dict[str, dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Rewrite non-ASCII / unstable keys to stable server_id; keep aliases.

    Returns (migrated_servers, warnings).
    Same URL/command fingerprints collapse onto the first canonical entry so
    user-level Chinese keys do not become ``dingtalk_robot_message_2``.
    """
    warnings: list[str] = []
    existing = set(servers.keys())
    for cfg in servers.values():
        sid = str(cfg.get("server_id") or "").strip()
        if sid:
            existing.add(sid)

    def _fingerprint(cfg: dict[str, Any]) -> str:
        url = str(cfg.get("url") or "").strip()
        if url:
            # Ignore volatile query keys when comparing gateway endpoints.
            return "url:" + url.split("?", 1)[0]
        cmd = str(cfg.get("command") or "").strip()
        args = " ".join(str(a) for a in (cfg.get("args") or []))
        return f"cmd:{cmd} {args}".strip()

    out: dict[str, dict[str, Any]] = {}
    fingerprints: dict[str, str] = {}

    def _priority(item: tuple[str, dict[str, Any]]) -> tuple[int, int, str]:
        name, cfg = item
        source = str(cfg.get("source") or "")
        source_rank = {"project": 0, "user": 1, "builtin": 2}.get(source, 3)
        sid = str(cfg.get("server_id") or "").strip()
        stable_rank = 0 if (sid and is_valid_server_id(sid) and (sid == name or is_valid_server_id(name))) else 1
        return (source_rank, stable_rank, name)

    for name, cfg in sorted(servers.items(), key=_priority):
        entry = dict(cfg)
        display = str(entry.get("display_name") or name)
        sid = str(entry.get("server_id") or "").strip()
        if not sid or not is_valid_server_id(sid):
            if is_valid_server_id(name):
                sid = name
            else:
                # Prefer known migration target even if already present as a key;
                # fingerprint dedupe below will collapse true duplicates.
                mapped = KNOWN_SERVER_ID_MIGRATIONS.get(display.strip()) or KNOWN_SERVER_ID_MIGRATIONS.get(
                    name.strip()
                )
                if mapped:
                    sid = mapped
                    warnings.append(f"MCP server '{name}' migrated to server_id '{sid}'")
                else:
                    sid = allocate_server_id(display if display != name else name, existing)
                    warnings.append(f"MCP server '{name}' migrated to server_id '{sid}'")
        fp = _fingerprint(entry)
        if fp and fp in fingerprints:
            kept = fingerprints[fp]
            warnings.append(
                f"MCP server '{name}' duplicates '{kept}' by endpoint; skipping"
            )
            kept_entry = out.get(kept)
            if kept_entry is not None and name != kept:
                aliases = list(kept_entry.get("aliases") or [])
                if name not in aliases:
                    aliases.append(name)
                kept_entry["aliases"] = aliases
            continue
        if sid in out:
            warnings.append(
                f"MCP server_id conflict: '{sid}' already used; keeping first entry"
            )
            continue
        existing.add(sid)
        entry["server_id"] = sid
        entry["display_name"] = display
        aliases = list(entry.get("aliases") or [])
        if name != sid and name not in aliases:
            aliases.append(name)
        entry["aliases"] = aliases
        out[sid] = entry
        if fp:
            fingerprints[fp] = sid
    return out, warnings


def _read_servers(path: Path) -> dict[str, dict[str, Any]]:
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for block_name in ("servers", "mcpServers"):
        block = raw.get(block_name)
        if not isinstance(block, dict):
            continue
        for name, cfg in block.items():
            if not isinstance(cfg, dict):
                continue
            entry = _normalize_entry(str(name), cfg)
            if entry is not None:
                out[str(name)] = entry
    for name, cfg in raw.items():
        if name in ("servers", "mcpServers") or not isinstance(cfg, dict):
            continue
        entry = _normalize_entry(str(name), cfg)
        if entry is not None:
            out[str(name)] = entry
    return out


def ensure_user_mcp_seeded() -> Path:
    """Create user mcp.json with codegraph seed if missing."""
    ensure_data_dirs()
    path = user_mcp_config_path()
    if not path.is_file():
        path.write_text(
            json.dumps({"servers": DEFAULT_MCP_SEED}, ensure_ascii=False, indent=2)
            + "\n",
            encoding="utf-8",
        )
    return path


def load_merged_mcp_servers(
    workspace: Path | str | None = None,
) -> dict[str, dict[str, Any]]:
    """Merge MCP configs: project overrides user overrides builtin seed.

    When server_id_v2 is enabled, keys are stable server_ids with display_name/aliases.
    """
    ensure_user_mcp_seeded()
    merged: dict[str, dict[str, Any]] = {
        k: {**v, "source": "builtin"} for k, v in DEFAULT_MCP_SEED.items()
    }
    for name, cfg in _read_servers(user_mcp_config_path()).items():
        merged[name] = {**cfg, "source": "user"}
    agent = project_agent_dir(workspace)
    if agent:
        for name, cfg in _read_servers(agent / "mcp.json").items():
            merged[name] = {**cfg, "source": "project"}
    if workspace:
        root_mcp = Path(workspace) / "mcp.json"
        if root_mcp.is_file():
            for name, cfg in _read_servers(root_mcp).items():
                merged[name] = {**cfg, "source": "project"}
    else:
        try:
            root_mcp = code_root() / "mcp.json"
            if root_mcp.is_file():
                for name, cfg in _read_servers(root_mcp).items():
                    merged[name] = {**cfg, "source": "project"}
        except Exception:
            pass

    try:
        from server.runtime.feature_flags import flag_enabled

        if flag_enabled("server_id_v2"):
            migrated, _warnings = migrate_server_keys(merged)
            with_aliases = dict(migrated)
            for sid, cfg in migrated.items():
                for alias in cfg.get("aliases") or []:
                    if alias and alias not in with_aliases:
                        with_aliases[str(alias)] = {**cfg, "server_id": sid}
            return with_aliases
    except Exception:
        pass
    return merged


def load_canonical_mcp_servers(
    workspace: Path | str | None = None,
) -> dict[str, dict[str, Any]]:
    """UI/catalog view: one row per stable server_id (no alias projections)."""
    servers = load_merged_mcp_servers(workspace)
    out: dict[str, dict[str, Any]] = {}
    for name, cfg in servers.items():
        server_id = str(cfg.get("server_id") or name)
        if name != server_id:
            continue
        if server_id in out:
            continue
        out[server_id] = cfg
    return out


def save_user_mcp_servers(servers: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    ensure_data_dirs()
    path = user_mcp_config_path()
    clean: dict[str, dict[str, Any]] = {}
    for name, cfg in servers.items():
        entry = _normalize_entry(str(name), cfg)
        if entry is None:
            continue
        clean[str(name)] = entry
    try:
        from server.runtime.feature_flags import flag_enabled

        if flag_enabled("server_id_v2"):
            clean, _ = migrate_server_keys(clean)
    except Exception:
        pass
    path.write_text(
        json.dumps({"servers": clean}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return clean


def upsert_user_mcp_server(name: str, cfg: dict[str, Any]) -> dict[str, Any]:
    servers = _read_servers(ensure_user_mcp_seeded())
    entry = _normalize_entry(name, cfg)
    if entry is None:
        raise ValueError(
            "MCP server entry requires a `command` (stdio) or `url` (streamable-http)"
        )
    servers[name] = entry
    saved = save_user_mcp_servers(servers)
    sid = str(entry.get("server_id") or name)
    stored = saved.get(sid) or saved.get(name) or entry
    return {**stored, "name": sid, "source": "user"}


def delete_user_mcp_server(name: str) -> bool:
    path = ensure_user_mcp_seeded()
    servers = _read_servers(path)
    target = name
    for sid, cfg in list(servers.items()):
        aliases = cfg.get("aliases") or []
        if sid == name or name in aliases or cfg.get("server_id") == name:
            target = sid
            break
    if target not in servers:
        return False
    del servers[target]
    save_user_mcp_servers(servers)
    return True


def substitute_workspace(value: str, workspace: Path | str | None) -> str:
    ws = str(Path(workspace).resolve()) if workspace else ""
    return value.replace("${workspaceFolder}", ws).replace("${workspace}", ws)


def resolve_mcp_launch(
    name: str,
    *,
    workspace: Path | str | None = None,
) -> dict[str, Any] | None:
    servers = load_merged_mcp_servers(workspace)
    cfg = servers.get(name)
    if not cfg:
        alt = KNOWN_SERVER_ID_MIGRATIONS.get(name)
        if alt:
            cfg = servers.get(alt)
    if not cfg:
        return None
    args = [substitute_workspace(a, workspace) for a in cfg.get("args") or []]
    env = {
        k: substitute_workspace(v, workspace) for k, v in (cfg.get("env") or {}).items()
    }
    description = str(cfg.get("description") or "")
    source = cfg.get("source") or "user"
    kind = str(cfg.get("type") or ("stdio" if cfg.get("command") else "streamable-http"))
    server_id = str(cfg.get("server_id") or name)
    display_name = str(cfg.get("display_name") or name)
    if cfg.get("url"):
        return {
            "name": server_id,
            "server_id": server_id,
            "display_name": display_name,
            "type": kind,
            "url": substitute_workspace(str(cfg["url"]), workspace),
            "description": description,
            "source": source,
        }
    command = substitute_workspace(str(cfg["command"]), workspace)
    which = shutil.which(command) if "/" not in command else command
    return {
        "name": server_id,
        "server_id": server_id,
        "display_name": display_name,
        "type": kind,
        "command": which or command,
        "args": args,
        "env": env,
        "description": description,
        "source": source,
    }
