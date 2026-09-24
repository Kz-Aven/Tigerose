"""User Hook configuration: validation, persistence, and scope resolution."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from avent_paths import data_root, project_agent_dir

HOOK_EVENTS = {
    "SessionStart", "RunStarted", "UserPromptSubmit", "PreModel", "PostModel",
    "StreamChunk", "ToolSelection", "PreToolUse", "PermissionRequest",
    "ToolExecute", "ToolProgress", "PostToolUse", "ToolError", "TurnEnd",
    "PreCompact", "SubagentStart", "SubagentStop", "Notification", "RunCancelled",
    "RunFinished", "TaskCompleted", "SessionEnd",
}
CONTROL_EVENTS = {"UserPromptSubmit", "PreToolUse"}
_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


def user_hooks_path() -> Path:
    return data_root() / "hooks.json"


def project_hooks_path(workspace: str | Path | None) -> Path | None:
    agent_dir = project_agent_dir(workspace)
    return agent_dir / "hooks.json" if agent_dir else None


def _read(path: Path | None) -> list[dict[str, Any]]:
    if not path or not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid hooks config: {path}") from exc
    if not isinstance(raw, dict) or raw.get("schema_version", 1) != 1:
        raise ValueError("hooks config schema_version must be 1")
    hooks = raw.get("hooks", [])
    if not isinstance(hooks, list):
        raise ValueError("hooks must be an array")
    return [dict(item) for item in hooks if isinstance(item, dict)]


def validate_hook(raw: dict[str, Any], *, hook_id: str | None = None) -> dict[str, Any]:
    hook = dict(raw)
    identifier = str(hook.get("hook_id") or hook_id or "").strip()
    if not _ID_RE.fullmatch(identifier):
        raise ValueError("hook_id must be lowercase letters, numbers, _ or -")
    event = str(hook.get("event") or "")
    if event not in HOOK_EVENTS:
        raise ValueError("unsupported hook event")
    handler = hook.get("handler")
    if not isinstance(handler, dict) or handler.get("type") != "command":
        raise ValueError("P0 supports only command handlers")
    command = str(handler.get("command") or "")
    if not command or not Path(command).is_absolute():
        raise ValueError("command must be an absolute executable path")
    args = handler.get("args", [])
    if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
        raise ValueError("handler args must be a string array")
    mode = str(handler.get("mode") or "observe")
    if mode not in {"observe", "decision"}:
        raise ValueError("handler mode must be observe or decision")
    if mode == "decision" and event not in CONTROL_EVENTS:
        raise ValueError("decision handlers are only valid for UserPromptSubmit and PreToolUse")
    timeout = float(handler.get("timeout_seconds", 10))
    if not 0 < timeout <= 60:
        raise ValueError("timeout_seconds must be between 0 and 60")
    profile = str(handler.get("payload_profile") or "tigerose-v1")
    if profile not in {"tigerose-v1", "codex-v1"}:
        raise ValueError("unsupported payload profile")
    failure = str(hook.get("failure_policy") or "allow")
    if failure not in {"allow", "deny"}:
        raise ValueError("failure_policy must be allow or deny")
    if event not in CONTROL_EVENTS and failure == "deny":
        raise ValueError("observe-only events cannot use failure_policy=deny")
    hook.update({
        "hook_id": identifier,
        "display_name": str(hook.get("display_name") or identifier),
        "enabled": bool(hook.get("enabled", True)),
        "event": event,
        "priority": int(hook.get("priority", 100)),
        "matcher": dict(hook.get("matcher") or {}),
        "failure_policy": failure,
        "handler": {**handler, "command": command, "args": args, "mode": mode,
                    "timeout_seconds": timeout, "payload_profile": profile},
        "description": str(hook.get("description") or ""),
    })
    return hook


def _write(path: Path, hooks: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema_version": 1, "hooks": hooks}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _trafficlight_hook(hook_id: str, display_name: str, event: str, codex_event: str, *, matcher: dict[str, list[str]] | None = None, priority: int = 100) -> dict[str, Any]:
    return {
        "hook_id": hook_id, "display_name": display_name, "enabled": True, "event": event,
        "priority": priority, "matcher": matcher or {}, "failure_policy": "allow",
        "handler": {"type": "command", "command": "/opt/homebrew/bin/trafficlight-codex-hook", "args": [], "mode": "observe", "payload_profile": "codex-v1", "codex_event_name": codex_event, "effect_kind": "trafficlight", "trafficlight_agent_name": "Tigerose", "trafficlight_keepalive": True, "timeout_seconds": 10},
        "description": "Tigerose 状态灯 Hook。启用后替代内建状态灯写入者。",
    }


def ensure_user_hooks_seeded() -> Path:
    """Seed or conservatively migrate Tigerose-owned TrafficLight Hooks."""
    path = user_hooks_path()
    if path.is_file():
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return path
        if not isinstance(document, dict) or document.get("schema_version", 1) != 1:
            return path
        hooks = document.get("hooks")
        if not isinstance(hooks, list):
            return path
        changed = False
        for hook in hooks:
            if not isinstance(hook, dict) or not str(hook.get("hook_id") or "").startswith("trafficlight-"):
                continue
            handler = hook.get("handler")
            if not isinstance(handler, dict):
                continue
            if handler.get("effect_kind") != "trafficlight" or handler.get("command") != "/opt/homebrew/bin/trafficlight-codex-hook":
                continue
            if handler.get("trafficlight_agent_name") != "Tigerose" or handler.get("trafficlight_keepalive") is not True:
                handler["trafficlight_agent_name"] = "Tigerose"
                handler["trafficlight_keepalive"] = True
                changed = True
        if changed:
            path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return path
    _write(path, [
        _trafficlight_hook("trafficlight-start", "状态灯：开始运行", "UserPromptSubmit", "UserPromptSubmit"),
        _trafficlight_hook("trafficlight-heartbeat", "状态灯：工具心跳", "PostToolUse", "PostToolUse"),
        _trafficlight_hook("trafficlight-complete", "状态灯：成功完成", "RunFinished", "Stop", matcher={"termination": ["completed", "partial"]}),
        _trafficlight_hook("trafficlight-idle", "状态灯：取消或阻断后空闲", "RunFinished", "Interrupt", matcher={"termination": ["failed", "budget_exhausted", "blocked_runtime", "blocked_user_action", "waiting_for_user"]}, priority=101),
        _trafficlight_hook("trafficlight-cancelled", "状态灯：取消后空闲", "RunCancelled", "Interrupt"),
        _trafficlight_hook("trafficlight-session-end", "状态灯：会话结束", "SessionEnd", "SessionEnd"),
    ])
    return path


def load_scope(scope: str, workspace: str | Path | None = None) -> list[dict[str, Any]]:
    if scope not in {"user", "project"}:
        raise ValueError("scope must be user or project")
    path = user_hooks_path() if scope == "user" else project_hooks_path(workspace)
    if scope == "project" and not path:
        raise ValueError("project scope requires workspace")
    return [validate_hook(item) for item in _read(path)]


def resolved_hooks(workspace: str | Path | None = None) -> list[dict[str, Any]]:
    merged = {item["hook_id"]: {**item, "source": "user", "overrides_global": False} for item in load_scope("user")}
    if workspace:
        for item in load_scope("project", workspace):
            merged[item["hook_id"]] = {**item, "source": "project", "overrides_global": item["hook_id"] in merged}
    return sorted(merged.values(), key=lambda item: (int(item["priority"]), item["hook_id"]))


def owns_trafficlight(hooks: list[dict[str, Any]]) -> bool:
    return any(hook.get("enabled") and (hook.get("handler") or {}).get("effect_kind") == "trafficlight" for hook in hooks)


def get_hook(hook_id: str, scope: str, workspace: str | Path | None = None) -> dict[str, Any] | None:
    return next((item for item in load_scope(scope, workspace) if item["hook_id"] == hook_id), None)


def put_hook(hook_id: str, raw: dict[str, Any], scope: str, workspace: str | Path | None = None) -> dict[str, Any]:
    hook = validate_hook(raw, hook_id=hook_id)
    path = user_hooks_path() if scope == "user" else project_hooks_path(workspace)
    if scope not in {"user", "project"} or not path:
        raise ValueError("project scope requires workspace")
    hooks = load_scope(scope, workspace)
    hooks = [item for item in hooks if item["hook_id"] != hook_id] + [hook]
    _write(path, hooks)
    return hook


def delete_hook(hook_id: str, scope: str, workspace: str | Path | None = None) -> bool:
    path = user_hooks_path() if scope == "user" else project_hooks_path(workspace)
    if scope not in {"user", "project"} or not path:
        raise ValueError("project scope requires workspace")
    hooks = load_scope(scope, workspace)
    kept = [item for item in hooks if item["hook_id"] != hook_id]
    if len(kept) == len(hooks):
        return False
    _write(path, kept)
    return True
