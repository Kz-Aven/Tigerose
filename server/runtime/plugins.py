"""Load bindable plugin extras: agent prompt fragments + optional tool handlers."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import yaml

from avent_paths import code_root, project_agent_dir, user_plugins_dir
from server.capabilities.catalog import ROOT


def _plugin_search_roots(workspace: Path | str | None = None) -> list[Path]:
    roots: list[Path] = []
    # Prefer user Application Support plugins (system install authority).
    roots.append(user_plugins_dir())
    proj = project_agent_dir(workspace)
    if proj is not None:
        roots.append(proj / "plugins")
    roots.append(ROOT / "plugins" if (ROOT / "plugins").is_dir() else ROOT)
    # Also allow code_root()/plugins and direct plugins/<name> under code root.
    roots.append(code_root() / "plugins")
    roots.append(ROOT)
    # Deduplicate while preserving order
    seen: set[str] = set()
    out: list[Path] = []
    for r in roots:
        key = str(r.resolve()) if r.exists() else str(r)
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def resolve_plugin_root(plugin_id: str, *, workspace: Path | str | None = None) -> Path | None:
    rel = plugin_id.removeprefix("plugin:")
    # Accept plugin:plugins/name or plugins/name or name
    name = rel
    if name.startswith("plugins/"):
        name = name[len("plugins/") :]
    name = name.strip("/")
    folder = name.split("/")[0]
    for base in _plugin_search_roots(workspace):
        candidates = [
            base / folder,
            base / "plugins" / folder,
            base / rel,
        ]
        for root in candidates:
            if (root / "plugin.yaml").is_file():
                return root.resolve()
    return None


def load_plugin_prompt_fragments(
    plugin_ids: list[str], *, workspace: Path | str | None = None
) -> list[str]:
    fragments: list[str] = []
    for pid in plugin_ids:
        root = resolve_plugin_root(pid, workspace=workspace)
        if root is None:
            continue
        manifest_path = root / "plugin.yaml"
        try:
            manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        agents = (manifest.get("agents") or {}).get("files") or []
        for f in agents:
            path = root / f
            if path.is_file():
                try:
                    fragments.append(
                        f"### Plugin agent: {manifest.get('name') or root.name}\n"
                        + path.read_text(encoding="utf-8")[:8000]
                    )
                except OSError:
                    continue
    return fragments


def load_plugin_tool_handlers(
    plugin_ids: list[str], *, workspace: Path | str | None = None
) -> tuple[list[dict], dict[str, Any]]:
    """Return OpenAI tool schemas + handlers for plugins that declare tools.module."""
    schemas: list[dict] = []
    handlers: dict[str, Any] = {}
    for pid in plugin_ids:
        root = resolve_plugin_root(pid, workspace=workspace)
        if root is None:
            continue
        manifest_path = root / "plugin.yaml"
        try:
            manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        tools_cfg = manifest.get("tools")
        module_file = None
        if isinstance(tools_cfg, dict):
            module_file = tools_cfg.get("module")
        elif isinstance(tools_cfg, str):
            module_file = tools_cfg
        if not module_file:
            continue
        mod_path = (root / module_file).resolve()
        try:
            mod_path.relative_to(root)
        except ValueError:
            continue
        if not mod_path.is_file():
            continue
        mod_name = f"avent_plugin_{root.name}_tools"
        spec = importlib.util.spec_from_file_location(mod_name, mod_path)
        if not spec or not spec.loader:
            continue
        mod = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(mod)
        except Exception:
            continue
        tool_defs = getattr(mod, "TOOL_DEFS", None) or getattr(mod, "TOOLS", None)
        mod_handlers = getattr(mod, "HANDLERS", None) or {}
        if not isinstance(tool_defs, list):
            continue
        prefix = root.name.replace("-", "_")
        for td in tool_defs:
            if not isinstance(td, dict):
                continue
            raw_name = td.get("name") or td.get("function", {}).get("name")
            if not raw_name:
                continue
            if td.get("type") == "function":
                schema = td
                fname = td["function"]["name"]
            else:
                fname = f"plugin__{prefix}__{raw_name}"
                schema = {
                    "type": "function",
                    "function": {
                        "name": fname,
                        "description": td.get("description") or raw_name,
                        "parameters": td.get("parameters")
                        or td.get("inputSchema")
                        or {"type": "object", "properties": {}},
                    },
                }
            schemas.append(schema)
            handler = mod_handlers.get(raw_name) or mod_handlers.get(fname)
            if handler:
                handlers[schema["function"]["name"]] = handler
    return schemas, handlers
