"""Capability catalog: 3-tier scan (project > user > builtin) + registry overlays."""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any

import yaml

from avent_paths import (
    code_root,
    data_root,
    project_agent_dir,
    user_plugins_dir,
    user_skills_dir,
)

ROOT = code_root()

# Builtin tools = executable registry (no phantom selectable tools).
from server.runtime.tools.registry import (  # noqa: E402
    DANGEROUS_TOOLS,
    catalog_entries,
)

BUILTIN_TOOL_CATALOG: list[dict[str, str]] = catalog_entries()

RUNTIME_REQUIRED_TOOLS: frozenset[str] = frozenset(
    {"read_file", "write_file", "edit_file", "bash", "get_skill"}
)

EMPTY_CAPABILITIES: dict[str, list[str]] = {
    "skills": [],
    "plugins": [],
    "tools": [],
    "mcp_servers": [],
}


def normalize_capabilities(raw: Any) -> dict[str, list[str]]:
    if not isinstance(raw, dict):
        raw = {}
    out = {k: list(raw.get(k) or []) for k in EMPTY_CAPABILITIES}
    for k, v in out.items():
        out[k] = [str(x).strip() for x in v if str(x).strip()]
    # Drop legacy non-executable tool names
    out["tools"] = [
        t.removeprefix("tool:")
        for t in out["tools"]
        if t.removeprefix("tool:") in {e["name"] for e in BUILTIN_TOOL_CATALOG}
    ]
    return out


def _read_frontmatter(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    if not text.startswith("---"):
        return {}
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}
    try:
        data = yaml.safe_load(parts[1]) or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _skill_id(rel: str) -> str:
    rel = rel.replace("\\", "/").removesuffix("/SKILL.md")
    if not rel.startswith("skills/"):
        rel = f"skills/{rel.removeprefix('skills/')}"
    return f"skill:{rel}"


def _plugin_id(rel: str) -> str:
    rel = rel.replace("\\", "/").removesuffix("/plugin.yaml")
    if not rel.startswith("plugins/"):
        rel = f"plugins/{rel.removeprefix('plugins/')}"
    return f"plugin:{rel}"


def _tool_id(name: str) -> str:
    return f"tool:{name}"


def _mcp_id(name: str) -> str:
    return f"mcp:{name}"


def _scan_skills_dir(skills_dir: Path, *, source: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not skills_dir.is_dir():
        return out
    for path in sorted(skills_dir.rglob("SKILL.md")):
        try:
            rel_under = path.parent.relative_to(skills_dir).as_posix()
        except ValueError:
            continue
        rel = f"skills/{rel_under}"
        fm = _read_frontmatter(path)
        name = str(fm.get("name") or path.parent.name)
        desc = str(fm.get("description") or "").strip() or f"Skill: {name}"
        out.append(
            {
                "id": _skill_id(rel),
                "kind": "skill",
                "name": name,
                "display_name": name,
                "description": desc[:240],
                "source_path": str(path.parent),
                "source": source,
                "from_plugin": None,
            }
        )
    return out


def scan_skills(root: Path | None = None) -> list[dict[str, Any]]:
    """Backward-compatible: scan a single root's skills/ (defaults to builtin)."""
    root = root or ROOT
    return _scan_skills_dir(root / "skills", source="builtin")


def _is_channel_plugin(manifest: dict[str, Any]) -> bool:
    ch = manifest.get("channel")
    if isinstance(ch, dict) and (ch.get("start") or ch.get("module")):
        return True
    return False


def _scan_plugins_dir(plugins_dir: Path, *, source: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not plugins_dir.is_dir():
        return out
    for path in sorted(plugins_dir.glob("*/plugin.yaml")):
        try:
            manifest = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        if not isinstance(manifest, dict):
            continue
        if _is_channel_plugin(manifest):
            continue
        folder = path.parent
        rel = f"plugins/{folder.name}"
        name = str(manifest.get("name") or folder.name)
        desc = str(manifest.get("description") or "").strip()
        if isinstance(desc, str):
            desc = re.sub(r"\s+", " ", desc).strip()
        nested: list[str] = []
        skills_root = folder / "skills"
        if skills_root.is_dir():
            for sp in sorted(skills_root.rglob("SKILL.md")):
                try:
                    nest_rel = sp.parent.relative_to(folder).as_posix()
                except ValueError:
                    continue
                nested.append(_skill_id(f"plugins/{folder.name}/{nest_rel}"))
        out.append(
            {
                "id": _plugin_id(rel),
                "kind": "plugin",
                "name": name,
                "display_name": name,
                "description": (desc or f"Plugin: {name}")[:240],
                "source_path": str(path),
                "source": source,
                "nested_skills": nested,
                "bindable": True,
            }
        )
    return out


def scan_plugins(root: Path | None = None) -> list[dict[str, Any]]:
    root = root or ROOT
    return _scan_plugins_dir(root / "plugins", source="builtin")


def scan_tools() -> list[dict[str, Any]]:
    out = []
    for t in BUILTIN_TOOL_CATALOG:
        name = t["name"]
        out.append(
            {
                "id": _tool_id(name),
                "kind": "tool",
                "name": name,
                "display_name": name,
                "description": t["description"],
                "source_path": "server/runtime/tools",
                "source": "builtin",
                "dangerous": name in DANGEROUS_TOOLS,
                "runtime_required": name in RUNTIME_REQUIRED_TOOLS,
                "deletable": False,
                "editable": False,
            }
        )
    return out


def scan_mcp(workspace: Path | str | None = None) -> list[dict[str, Any]]:
    from server.capabilities.mcp_config import load_canonical_mcp_servers

    servers = load_canonical_mcp_servers(workspace)
    out = []
    for name, cfg in sorted(servers.items()):
        source = str(cfg.get("source") or "user")
        display = str(cfg.get("display_name") or name)
        out.append(
            {
                "id": _mcp_id(name),
                "kind": "mcp",
                "name": name,
                "display_name": display,
                "description": str(cfg.get("description") or f"MCP: {name}")[:240],
                "source_path": "mcp.json",
                "source": source,
                "meta": {
                    "type": cfg.get("type"),
                    "command": cfg.get("command"),
                    "args": cfg.get("args") or [],
                    "env": cfg.get("env") or {},
                    "url": cfg.get("url"),
                    "server_id": name,
                    "aliases": list(cfg.get("aliases") or []),
                },
            }
        )
    return out


def _merge_by_id(*layers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Later layers override earlier ones (call with builtin, user, project)."""
    by_id: dict[str, dict[str, Any]] = {}
    for layer in layers:
        for item in layer:
            by_id[item["id"]] = dict(item)
    return list(by_id.values())


def scan_all(
    root: Path | None = None,
    *,
    workspace: Path | str | None = None,
) -> list[dict[str, Any]]:
    """Scan builtin + user + optional project `.agent/` (project wins)."""
    # `root` kept for callers that pass code root; builtin always uses code_root.
    _ = root
    builtin_skills = _scan_skills_dir(code_root() / "skills", source="builtin")
    user_skills = _scan_skills_dir(user_skills_dir(), source="user")
    project_skills: list[dict[str, Any]] = []
    agent = project_agent_dir(workspace)
    if agent and (agent / "skills").is_dir():
        project_skills = _scan_skills_dir(agent / "skills", source="project")

    builtin_plugins = _scan_plugins_dir(code_root() / "plugins", source="builtin")
    user_plugins = _scan_plugins_dir(user_plugins_dir(), source="user")
    project_plugins: list[dict[str, Any]] = []
    if agent and (agent / "plugins").is_dir():
        project_plugins = _scan_plugins_dir(agent / "plugins", source="project")

    skills = _merge_by_id(builtin_skills, user_skills, project_skills)
    plugins = _merge_by_id(builtin_plugins, user_plugins, project_plugins)
    return skills + plugins + scan_tools() + scan_mcp(workspace)


def annotate_management_flags(catalog: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add deletable/editable for Settings UI."""
    out = []
    for raw in catalog:
        item = dict(raw)
        kind = item.get("kind")
        source = item.get("source") or "builtin"
        if kind == "tool":
            item["runtime_required"] = item.get("name") in RUNTIME_REQUIRED_TOOLS
            item["deletable"] = False
            item["editable"] = False
        elif kind == "mcp":
            item["deletable"] = source in ("user", "builtin")
            item["editable"] = source == "user"
        elif kind in ("skill", "plugin"):
            # Settings: project read-only; user real-delete; builtin tombstone
            item["deletable"] = source in ("user", "builtin")
            item["editable"] = source in ("user", "builtin")
        else:
            item["deletable"] = False
            item["editable"] = False
        out.append(item)
    return out


def apply_registry(
    scanned: list[dict[str, Any]],
    registry_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Merge registry overlays; hidden items dropped from visible catalog."""
    by_id = {item["id"]: dict(item) for item in scanned}
    for row in registry_rows:
        cid = str(row.get("capability_id") or "").strip()
        if not cid:
            continue
        base = by_id.get(cid) or {
            "id": cid,
            "kind": row.get("kind") or "mcp",
            "name": cid.split(":", 1)[-1],
            "display_name": cid.split(":", 1)[-1],
            "description": "",
            "source_path": row.get("source_path") or "",
            "source": "user",
        }
        if row.get("display_name"):
            base["display_name"] = row["display_name"]
        if row.get("description"):
            base["description"] = row["description"]
        if row.get("source_path"):
            base["source_path"] = row["source_path"]
        if row.get("meta"):
            base["meta"] = row["meta"]
        if int(row.get("hidden") or 0):
            by_id.pop(cid, None)
            continue
        by_id[cid] = base
    return sorted(by_id.values(), key=lambda x: (x.get("kind", ""), x.get("display_name", "")))


def resolve_bundle(
    capabilities: dict[str, list[str]],
    catalog: list[dict[str, Any]],
) -> dict[str, Any]:
    """Resolve template allowlist against catalog; mark missing ids."""
    caps = normalize_capabilities(capabilities)
    by_id = {c["id"]: c for c in catalog}
    tool_by_name = {c["name"]: c for c in catalog if c.get("kind") == "tool"}

    def resolve_list(ids: list[str], kind: str) -> list[dict[str, Any]]:
        resolved = []
        for raw in ids:
            cid = raw
            item = by_id.get(cid)
            if not item and kind == "tool":
                name = raw.removeprefix("tool:")
                item = tool_by_name.get(name)
                cid = item["id"] if item else _tool_id(name)
            if not item and kind == "mcp" and not raw.startswith("mcp:"):
                item = by_id.get(_mcp_id(raw))
                cid = item["id"] if item else _mcp_id(raw)
            if not item and kind == "skill" and not raw.startswith("skill:"):
                for c in catalog:
                    if c.get("kind") == "skill" and c.get("name") == raw:
                        item = c
                        cid = c["id"]
                        break
            if item:
                resolved.append({**item, "missing": False})
            else:
                resolved.append(
                    {
                        "id": cid,
                        "kind": kind,
                        "name": cid,
                        "display_name": cid,
                        "description": "",
                        "missing": True,
                    }
                )
        return resolved

    skills = resolve_list(caps["skills"], "skill")
    plugins = resolve_list(caps["plugins"], "plugin")
    for p in plugins:
        if p.get("missing"):
            continue
        for nid in p.get("nested_skills") or []:
            if any(s["id"] == nid for s in skills):
                continue
            item = by_id.get(nid)
            if item:
                skills.append({**item, "missing": False, "from_plugin": p["id"]})
            else:
                skills.append(
                    {
                        "id": nid,
                        "kind": "skill",
                        "name": nid.split("/")[-1],
                        "display_name": nid.split("/")[-1],
                        "description": f"来自插件 {p.get('display_name')}",
                        "missing": False,
                        "from_plugin": p["id"],
                    }
                )

    return {
        "skills": skills,
        "plugins": plugins,
        "tools": resolve_list(caps["tools"], "tool"),
        "mcp_servers": resolve_list(caps["mcp_servers"], "mcp"),
        "raw": caps,
    }


def _skill_rel(sid: str) -> str:
    rel = sid.removeprefix("skill:")
    return rel.removeprefix("skills/") if rel.startswith("skills/") else rel


def format_skill_context(skill: dict[str, str]) -> str:
    """Expose the resource base together with instructions, including paths with spaces."""
    path = Path(skill["path"]).resolve()
    return (
        f"### Skill: {skill.get('id', '')}\n"
        f"SKILL.md absolute path: {path}\n"
        f"Skill resource directory: {path.parent}\n"
        "Resolve relative script/reference paths and <skill-dir> against this directory, "
        "not the workspace. Quote paths containing spaces in shell commands. "
        "Do not search unrelated directories to locate this skill.\n\n"
        + skill.get("content", "")
    )


def load_skill_texts(
    skill_ids: list[str],
    root: Path | None = None,
    *,
    workspace: Path | str | None = None,
) -> list[dict[str, str]]:
    """Load SKILL.md bodies: project > user > builtin."""
    _ = root
    loaded: list[dict[str, str]] = []
    agent = project_agent_dir(workspace)
    for sid in skill_ids:
        rest = _skill_rel(sid)
        candidates: list[Path] = []
        if agent:
            candidates.append(agent / "skills" / rest / "SKILL.md")
        candidates.append(user_skills_dir() / rest / "SKILL.md")
        candidates.append(code_root() / "skills" / rest / "SKILL.md")
        # Legacy ids pointing at full relative paths under code root
        rel = sid.removeprefix("skill:")
        candidates.append(code_root() / rel / "SKILL.md")
        path = next((p for p in candidates if p.is_file()), None)
        if path is None:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        loaded.append({"id": sid, "path": str(path), "content": text[:12000]})
    return loaded


def import_skill_from_path(src: Path) -> dict[str, Any]:
    """Copy a skill folder (containing SKILL.md) into user skills dir."""
    src = Path(src).expanduser().resolve()
    if src.is_file() and src.name == "SKILL.md":
        src = src.parent
    skill_md = src / "SKILL.md"
    if not skill_md.is_file():
        raise ValueError("导入路径需为包含 SKILL.md 的技能目录")
    name = src.name
    dest = user_skills_dir() / name
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest)
    items = _scan_skills_dir(user_skills_dir(), source="user")
    match = next((i for i in items if Path(i["source_path"]).resolve() == dest.resolve()), None)
    if not match:
        match = next((i for i in items if i["name"] == name or dest.name in i["id"]), items[-1])
    return match


def import_plugin_from_path(src: Path) -> dict[str, Any]:
    src = Path(src).expanduser().resolve()
    if src.is_file() and src.name == "plugin.yaml":
        src = src.parent
    manifest = src / "plugin.yaml"
    if not manifest.is_file():
        raise ValueError("导入路径需为包含 plugin.yaml 的插件目录")
    name = src.name
    dest = user_plugins_dir() / name
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest)
    items = _scan_plugins_dir(user_plugins_dir(), source="user")
    match = next((i for i in items if i["id"] == _plugin_id(f"plugins/{name}")), None)
    if not match:
        match = items[-1]
    return match


def delete_capability_files(item: dict[str, Any]) -> None:
    """Physically delete user-level skill/plugin directory."""
    source = item.get("source")
    kind = item.get("kind")
    if source != "user" or kind not in ("skill", "plugin"):
        raise ValueError("仅用户级技能/插件支持文件删除")
    path = Path(str(item.get("source_path") or ""))
    if kind == "plugin" and path.is_file():
        path = path.parent
    if not path.exists():
        return
    if kind == "skill" and path.is_dir():
        # safety: must be under user skills
        user_root = user_skills_dir().resolve()
        if user_root not in path.resolve().parents and path.resolve() != user_root:
            if not str(path.resolve()).startswith(str(user_root)):
                raise ValueError("拒绝删除用户目录外的路径")
        shutil.rmtree(path)
    elif kind == "plugin" and path.is_dir():
        user_root = user_plugins_dir().resolve()
        if not str(path.resolve()).startswith(str(user_root)):
            raise ValueError("拒绝删除用户目录外的路径")
        shutil.rmtree(path)
