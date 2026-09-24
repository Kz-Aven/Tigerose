"""Install, inspect, and validate local Agent Asset Expert components."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

from .adapters import adapters
from .storage import AssetStore, data_root


def _manifest_path() -> Path:
    return data_root() / "installation-state.json"


def _load_manifest() -> dict[str, Any]:
    path = _manifest_path()
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {"version": 1, "platforms": {}}


def _save_manifest(value: dict[str, Any]) -> None:
    path = _manifest_path(); path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


_CODEX_BEGIN = "# BEGIN agent-asset-expert"
_CODEX_END = "# END agent-asset-expert"
_HOOK_COMMAND = "agent-asset-expert hook --platform {platform}"
_WORKBUDDY_EVENTS = ("UserPromptSubmit", "PostToolUse", "Stop", "Interrupt", "SessionEnd")


def _command(platform: str) -> str:
    executable = shutil.which("agent-asset-expert") or str(Path(sys.argv[0]).resolve())
    return f"{executable} hook --platform {platform}"


def _owns_hook(command: Any, platform: str) -> bool:
    parts = str(command).split()
    return len(parts) == 4 and Path(parts[0]).name == "agent-asset-expert" and parts[1:] == ["hook", "--platform", platform]


def _install_codex_mcp(config_path: Path) -> dict[str, str]:
    original = config_path.read_text(encoding="utf-8")
    if _CODEX_BEGIN in original:
        return {"path": str(config_path), "backup": ""}
    backup = data_root() / "backups" / "codex-config.toml"
    backup.parent.mkdir(parents=True, exist_ok=True)
    backup.write_text(original, encoding="utf-8")
    block = f"\n{_CODEX_BEGIN}\n[mcp_servers.agent_asset_expert]\ncommand = \"agent-asset-expert\"\nargs = [\"mcp\"]\n{_CODEX_END}\n"
    config_path.write_text(original.rstrip() + block, encoding="utf-8")
    return {"path": str(config_path), "backup": str(backup)}


def _uninstall_codex_mcp(config_path: Path) -> None:
    if not config_path.is_file():
        return
    original = config_path.read_text(encoding="utf-8")
    if _CODEX_BEGIN not in original or _CODEX_END not in original:
        return
    before, tail = original.split(_CODEX_BEGIN, 1)
    _, after = tail.split(_CODEX_END, 1)
    config_path.write_text((before.rstrip() + after).lstrip("\n"), encoding="utf-8")


def _install_tigerose_mcp(path: Path) -> dict[str, str]:
    value = json.loads(path.read_text(encoding="utf-8"))
    servers = value.get("servers") if isinstance(value, dict) else None
    server = servers.get("agent_assets") if isinstance(servers, dict) else None
    if not isinstance(server, dict):
        raise ValueError("Tigerose mcp.json must contain servers.agent_assets")
    backup = data_root() / "backups" / "tigerose-mcp.json"
    backup.parent.mkdir(parents=True, exist_ok=True)
    backup.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    server.update({"type": "stdio", "command": "agent-asset-expert", "args": ["mcp"], "env": {}, "description": "本机只读 Agent 数据资产查询（Tigerose / Codex / WorkBuddy）", "display_name": "agent_assets", "server_id": "agent_assets", "aliases": []})
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"path": str(path), "backup": str(backup)}


def _uninstall_tigerose_mcp(path: Path, backup_path: Path) -> None:
    if not path.is_file() or not backup_path.is_file():
        return
    current = json.loads(path.read_text(encoding="utf-8"))
    backup = json.loads(backup_path.read_text(encoding="utf-8"))
    servers = current.get("servers") if isinstance(current, dict) else None
    prior_servers = backup.get("servers") if isinstance(backup, dict) else None
    current_server = servers.get("agent_assets") if isinstance(servers, dict) else None
    prior_server = prior_servers.get("agent_assets") if isinstance(prior_servers, dict) else None
    if isinstance(current_server, dict) and current_server.get("command") == "agent-asset-expert" and isinstance(prior_server, dict):
        servers["agent_assets"] = prior_server
        path.write_text(json.dumps(current, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _install_workbuddy_hooks(settings_path: Path) -> dict[str, str]:
    value = json.loads(settings_path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("WorkBuddy settings.json must be an object")
    backup = data_root() / "backups" / "workbuddy-settings.json"
    backup.parent.mkdir(parents=True, exist_ok=True)
    backup.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    hooks = value.setdefault("hooks", {})
    command = _command("workbuddy")
    for event in _WORKBUDDY_EVENTS:
        matchers = hooks.setdefault(event, [])
        if not isinstance(matchers, list):
            raise ValueError(f"WorkBuddy hooks.{event} must be a list")
        existing = [child for matcher in matchers if isinstance(matcher, dict) for child in matcher.get("hooks", []) if isinstance(matcher.get("hooks"), list) and isinstance(child, dict) and _owns_hook(child.get("command"), "workbuddy")]
        if existing:
            for child in existing:
                child["command"] = command
        else:
            matchers.append({"hooks": [{"type": "command", "command": command}]})
    settings_path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"path": str(settings_path), "backup": str(backup)}


def _uninstall_workbuddy_hooks(settings_path: Path) -> None:
    if not settings_path.is_file():
        return
    value = json.loads(settings_path.read_text(encoding="utf-8"))
    hooks = value.get("hooks") if isinstance(value, dict) else None
    if not isinstance(hooks, dict):
        return
    for event in _WORKBUDDY_EVENTS:
        matchers = hooks.get(event)
        if not isinstance(matchers, list):
            continue
        kept = []
        for matcher in matchers:
            if not isinstance(matcher, dict):
                kept.append(matcher); continue
            children = matcher.get("hooks")
            if not isinstance(children, list):
                kept.append(matcher); continue
            remaining = [child for child in children if not (isinstance(child, dict) and _owns_hook(child.get("command"), "workbuddy"))]
            if remaining:
                kept.append({**matcher, "hooks": remaining})
        hooks[event] = kept
    settings_path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _install_workbuddy_mcp(path: Path) -> dict[str, str]:
    value = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    if not isinstance(value, dict):
        raise ValueError("WorkBuddy mcp.json must be an object")
    backup = data_root() / "backups" / "workbuddy-mcp.json"
    backup.parent.mkdir(parents=True, exist_ok=True)
    backup.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    servers = value.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise ValueError("WorkBuddy mcp.json mcpServers must be an object")
    servers["agent_asset_expert"] = {"command": shutil.which("agent-asset-expert") or str(Path(sys.argv[0]).resolve()), "args": ["mcp"], "disabled": False}
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"path": str(path), "backup": str(backup)}


def _uninstall_workbuddy_mcp(path: Path, backup_path: Path) -> None:
    if not path.is_file(): return
    value = json.loads(path.read_text(encoding="utf-8")); servers = value.get("mcpServers", {})
    if not isinstance(servers, dict) or "agent_asset_expert" not in servers: return
    backup = json.loads(backup_path.read_text(encoding="utf-8")) if backup_path.is_file() else {}
    prior = backup.get("mcpServers", {}).get("agent_asset_expert") if isinstance(backup, dict) else None
    if prior is None: servers.pop("agent_asset_expert")
    else: servers["agent_asset_expert"] = prior
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _install_codex_hooks(path: Path) -> dict[str, str]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get("hooks"), dict):
        raise ValueError("Codex hooks.json must contain a hooks object")
    backup = data_root() / "backups" / "codex-hooks.json"
    backup.parent.mkdir(parents=True, exist_ok=True)
    backup.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    command = _HOOK_COMMAND.format(platform="codex")
    for event, matchers in value["hooks"].items():
        if not isinstance(matchers, list):
            continue
        present = any(isinstance(child, dict) and child.get("command") == command for matcher in matchers if isinstance(matcher, dict) for child in matcher.get("hooks", []) if isinstance(matcher.get("hooks"), list))
        if not present:
            matchers.append({"hooks": [{"type": "command", "command": command, "async": True}]})
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"path": str(path), "backup": str(backup)}


def _uninstall_codex_hooks(path: Path) -> None:
    if not path.is_file():
        return
    value = json.loads(path.read_text(encoding="utf-8"))
    hooks = value.get("hooks") if isinstance(value, dict) else None
    if not isinstance(hooks, dict):
        return
    command = _HOOK_COMMAND.format(platform="codex")
    for event, matchers in hooks.items():
        if not isinstance(matchers, list):
            continue
        kept = []
        for matcher in matchers:
            if not isinstance(matcher, dict) or not isinstance(matcher.get("hooks"), list):
                kept.append(matcher); continue
            remaining = [child for child in matcher["hooks"] if not (isinstance(child, dict) and child.get("command") == command)]
            if remaining:
                kept.append({**matcher, "hooks": remaining})
        hooks[event] = kept
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def install(platform: str) -> int:
    available = adapters()
    targets = available.values() if platform == "auto" else [available[platform]]
    manifest = _load_manifest()
    results = []
    for adapter in targets:
        result = adapter.install()
        detail = result.__dict__.copy()
        if result.detected and adapter.platform == "codex" and result.mcp_config:
            detail["owned_mcp_config"] = _install_codex_mcp(Path(result.mcp_config))
        if result.detected and adapter.platform == "tigerose" and result.mcp_config:
            detail["owned_mcp_config"] = _install_tigerose_mcp(Path(result.mcp_config))
        if result.detected and adapter.platform == "codex" and result.hook_config:
            detail["owned_hook_config"] = _install_codex_hooks(Path(result.hook_config))
        if result.detected and adapter.platform == "workbuddy" and result.mcp_config:
            detail["owned_hook_config"] = _install_workbuddy_hooks(Path(result.hook_config))
            detail["owned_mcp_config"] = _install_workbuddy_mcp(Path(result.mcp_config))
        results.append(detail)
        if result.detected:
            manifest["platforms"][adapter.platform] = detail
    _save_manifest(manifest)
    print(json.dumps({"results": results, "data_root": str(data_root())}, ensure_ascii=False, indent=2))
    return 0 if any(item["detected"] for item in results) else 2


def doctor(platform: str) -> int:
    manifest = _load_manifest()
    selected = manifest["platforms"] if platform == "auto" else {platform: manifest["platforms"].get(platform)}
    selected = {name: value for name, value in selected.items() if value}
    store = AssetStore()
    assistants = store.list_assistants()
    platform_health = {name: any(item["platform"] == name for item in assistants) for name in selected}
    report = {"installed_platforms": selected, "registered_assistants": assistants, "platform_health": platform_health, "mcp_command": ["agent-asset-expert", "mcp"], "healthy": bool(selected) and all(platform_health.values()), "required_action": "Run one real Agent task on each installed platform to verify collection" if any(not value for value in platform_health.values()) else ""}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["healthy"] else 2


def uninstall(platform: str) -> int:
    manifest = _load_manifest()
    installed = manifest["platforms"].pop(platform, None)
    if not installed:
        print(json.dumps({"platform": platform, "removed": False, "reason": "not installed"}, ensure_ascii=False))
        return 2
    hook = data_root() / "hooks" / f"{platform}.json"
    if hook.is_file():
        hook.unlink()
    config = installed.get("owned_mcp_config", {}).get("path")
    if config:
        if platform == "tigerose":
            _uninstall_tigerose_mcp(Path(config), Path(installed["owned_mcp_config"]["backup"]))
        elif platform == "workbuddy":
            _uninstall_workbuddy_mcp(Path(config), Path(installed["owned_mcp_config"]["backup"]))
        else:
            _uninstall_codex_mcp(Path(config))
    hook_config = installed.get("owned_hook_config", {}).get("path")
    if hook_config:
        (_uninstall_codex_hooks if platform == "codex" else _uninstall_workbuddy_hooks)(Path(hook_config))
    _save_manifest(manifest)
    print(json.dumps({"platform": platform, "removed": True, "data_retained": True}, ensure_ascii=False))
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="agent-asset-expert")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("install", "doctor", "uninstall"):
        child = commands.add_parser(name); child.add_argument("--platform", choices=["auto", "tigerose", "codex", "workbuddy"], default="auto")
    commands.add_parser("status")
    commands.add_parser("mcp")
    hook = commands.add_parser("hook")
    hook.add_argument("--platform", choices=["codex", "workbuddy"], required=True)
    args = parser.parse_args()
    if args.command == "install": raise SystemExit(install(args.platform))
    if args.command == "doctor": raise SystemExit(doctor(args.platform))
    if args.command == "uninstall": raise SystemExit(uninstall(args.platform))
    if args.command == "status": print(json.dumps(_load_manifest(), ensure_ascii=False, indent=2)); return
    if args.command == "mcp":
        from .mcp_server import main as serve
        serve()
    if args.command == "hook":
        os.environ["AGENT_ASSET_EXPERT_PLATFORM"] = args.platform
        from .collector import main as collect
        collect()


if __name__ == "__main__":
    main()
