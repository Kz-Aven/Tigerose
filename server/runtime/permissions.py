"""Human-in-the-loop permission gate for elevated tool calls."""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from server.api.sse_bus import bus
from server.runtime.policy import (
    DANGER_DENY,
    FILE_ACCESS_ASK,
    FILE_ACCESS_FULL,
    FILE_ACCESS_WORKSPACE,
    PATH_TOOLS,
    PermissionPolicy,
    parse_permission_policy,
)
from server.runtime.tools import files as file_tools
from server.scheduler import group_scheduler as scheduler

log = logging.getLogger("avent.permissions")

_TIMEOUT_S = 600.0
_lock = threading.Lock()
_pending: dict[str, "PendingPermission"] = {}

# Gate decision actions for the tool loop.
ACTION_ASK = "ask"
ACTION_DENY = "deny"
ACTION_ALLOW = "allow"  # elevate without prompting (e.g. file_access=full)


@dataclass
class PendingPermission:
    request_id: str
    channel: str
    tool: str
    reason: str
    detail: str
    event: threading.Event = field(default_factory=threading.Event)
    approved: bool | None = None
    # once | always — meaningful when approved is True
    mode: str = "once"
    domain: str = ""
    args_preview: dict[str, Any] = field(default_factory=dict)
    timeout_s: float = _TIMEOUT_S
    approval_choices: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)


def classify_permission(
    name: str,
    args: dict[str, Any],
    *,
    cwd: Path,
    policy: PermissionPolicy | None = None,
    skill_roots: tuple[Path, ...] = (),
    trusted_roots: tuple[Path, ...] = (),
    connector_executables: tuple[str, ...] = (),
) -> dict[str, str] | None:
    gate = _classify_permission(
        name,
        args,
        cwd=cwd,
        policy=policy,
        skill_roots=skill_roots,
        trusted_roots=trusted_roots,
        connector_executables=connector_executables,
    )
    if name != "edit_file":
        return gate
    try:
        target = file_tools.resolve_path(cwd, str(args.get("path") or ""))
    except Exception:
        target = None
    if target is not None and file_tools.is_trusted_path(cwd, target, trusted_roots):
        return gate
    return combine_gates({
        "action": ACTION_ASK,
        "reason": "scope_file_edit",
        "detail": f"助理请求修改已有文件：{args.get('path', '')}。请确认本次编辑。",
        "domain": "runtime.file.edit",
        "approval_choices": "once",
    }, gate)


def _classify_permission(
    name: str,
    args: dict[str, Any],
    *,
    cwd: Path,
    policy: PermissionPolicy | None = None,
    skill_roots: tuple[Path, ...] = (),
    trusted_roots: tuple[Path, ...] = (),
    connector_executables: tuple[str, ...] = (),
) -> dict[str, str] | None:
    """Return a gate decision or None if the call may proceed without elevation.

    Decision shape when not None:
      {action: ask|deny|allow, reason, detail}
    """
    pol = policy or parse_permission_policy({})
    cwd_r = Path(cwd).resolve()

    # Session clear must never be done via silent localhost curl.
    if name == "bash":
        command = str(args.get("command") or "")
        lowered = command.lower()
        if "sessions/clear" in lowered or "/sessions/clear" in lowered:
            return {
                "action": ACTION_DENY,
                "reason": "session_clear",
                "detail": (
                    "该操作未执行：清空聊天必须通过确认弹窗完成，"
                    "不能通过终端命令绕过确认。\n"
                    "请使用“清空当前会话”，再由用户明确确认。"
                ),
            }

        # Managed connector CLIs are already resolved from this assistant's enabled,
        # authenticated connector set and execute through the argv-only adapter.
        from server.runtime.scope_guard import _is_connector_cli_command

        if _is_connector_cli_command(command, list(connector_executables)):
            return None

    # Axis B first — dangerous ops
    if name == "bash" and pol.monitors_dangerous_bash():
        command = str(args.get("command") or "")
        if file_tools.bash_needs_permission(command):
            detail = (
                "助理请求执行可能危险的 shell 命令：\n"
                f"```\n{command.strip()[:800]}\n```"
            )
            if pol.danger_policy == DANGER_DENY:
                return {
                    "action": ACTION_DENY,
                    "reason": "dangerous_bash",
                    "detail": detail + "\n\n该命令未执行：当前助理的安全策略禁止高风险终端操作。",
                }
            # ask
            return {
                "action": ACTION_ASK,
                "reason": "dangerous_bash",
                "detail": detail,
            }

    if name == "request_clear_session":
        # Always ask — never auto-allow.
        reason_text = str(args.get("reason") or "助理请求清空当前对话")[:500]
        return {
            "action": ACTION_ASK,
            "reason": "session_clear",
            "detail": (
                "助理请求清空当前会话的聊天记录。\n"
                f"原因：{reason_text}\n\n"
                "批准后将先写入不可变归档快照，再清空界面消息。"
            ),
        }

    if name == "bash":
        external = [
            path
            for path in file_tools.bash_absolute_paths(
                str(args.get("command") or ""),
                skip_argument_index=file_tools.skill_script_argument_index(
                    str(args.get("command") or ""), cwd=cwd_r, skill_roots=skill_roots
                ),
                cwd=cwd_r,
            )
            if not file_tools.is_trusted_path(cwd_r, path, trusted_roots)
        ]
        if external:
            detail = (
                "助理请求通过 shell 访问工作区外的路径：\n"
                + "\n".join(f"- `{path}`" for path in external[:10])
                + f"\n（当前工作区：`{cwd_r}`）"
            )
            if pol.file_access == FILE_ACCESS_WORKSPACE:
                return {
                    "action": ACTION_DENY,
                    "reason": "external_path",
                    "detail": detail + "\n\n该操作未执行：目标文件不在当前项目目录内。"
                    "请将文件放入项目目录，或调整文件访问范围。",
                }
            if pol.file_access == FILE_ACCESS_FULL:
                return {
                    "action": ACTION_ALLOW,
                    "reason": "external_path",
                    "detail": detail,
                }
            return {
                "action": ACTION_ASK,
                "reason": "external_path",
                "detail": detail,
            }

    # Axis A — path tools outside workspace
    if name in PATH_TOOLS:
        path_arg = str(args.get("path") or "").strip()
        if not path_arg:
            return None
        try:
            target = file_tools.resolve_path(cwd_r, path_arg)
        except Exception as exc:  # noqa: BLE001
            return {
                "action": ACTION_ASK if pol.file_access == FILE_ACCESS_ASK else (
                    ACTION_ALLOW if pol.file_access == FILE_ACCESS_FULL else ACTION_DENY
                ),
                "reason": "external_path",
                "detail": f"无法解析路径 `{path_arg}`：{exc}",
            }
        if file_tools.is_trusted_path(cwd_r, target, trusted_roots):
            return None
        if name in {"read_file", "excel_read"} and file_tools.is_skill_resource(target, skill_roots):
            return {
                "action": ACTION_ALLOW,
                "reason": "skill_resource_read",
                "detail": f"读取已启用技能的资源：`{target}`",
                "allow_external": "true",
            }
        action_label = {
            "read_file": "读取",
            "write_file": "写入",
            "edit_file": "编辑",
            "excel_read": "读取 Excel",
            "excel_write": "写入 Excel",
        }.get(name, "访问")
        detail = (
            f"助理请求{action_label}工作区外的文件：\n`{target}`\n"
            f"（当前工作区：`{cwd_r}`）"
        )
        if pol.file_access == FILE_ACCESS_WORKSPACE:
            return {
                "action": ACTION_DENY,
                "reason": "external_path",
                "detail": detail + "\n\n该操作未执行：目标文件不在当前项目目录内。"
                "请将文件放入项目目录，或调整文件访问范围。",
            }
        if pol.file_access == FILE_ACCESS_FULL:
            return {
                "action": ACTION_ALLOW,
                "reason": "external_path",
                "detail": detail,
            }
        # ask (default)
        return {
            "action": ACTION_ASK,
            "reason": "external_path",
            "detail": detail,
        }

    return None


def combine_gates(
    scope_gate: dict[str, str] | None,
    policy_gate: dict[str, str] | None,
) -> dict[str, str] | None:
    """Require both approvals; policy denials cannot be overridden by scope grants."""
    if not scope_gate:
        return policy_gate
    if not policy_gate:
        return scope_gate
    if scope_gate == policy_gate:
        return scope_gate
    if policy_gate.get("action") == ACTION_DENY:
        return policy_gate
    combined = dict(scope_gate)
    combined["detail"] = scope_gate.get("detail", "") + "\n\n" + policy_gate.get("detail", "")
    if policy_gate.get("reason") == "external_path":
        combined["allow_external"] = "true"
    elif policy_gate.get("allow_external"):
        combined["allow_external"] = "true"
    if policy_gate.get("reason") == "dangerous_bash":
        combined["allow_dangerous_bash"] = "true"
    # Approval of a compound request never grants either axis for future calls.
    combined["approval_choices"] = "once"
    return combined


def request_and_wait(
    *,
    channel: str,
    tool: str,
    args: dict[str, Any],
    reason: str,
    detail: str,
    timeout_s: float = _TIMEOUT_S,
    domain: str = "",
    approval_choices: str = "",
) -> dict[str, Any]:
    """Publish permission_request over SSE and block until resolve/timeout.

    Returns ``{"approved": bool, "mode": "once"|"always"}``.
    """
    from server.runtime.mesh_runtime import permission_request

    mesh_decision = permission_request(tool=tool, args=args, reason=reason, detail=detail)
    if mesh_decision is not None:
        return mesh_decision
    request_id = uuid.uuid4().hex[:16]
    choices = [
        part.strip()
        for part in str(approval_choices or "").split(",")
        if part.strip()
    ]
    pending = PendingPermission(
        request_id=request_id,
        channel=channel,
        tool=tool,
        reason=reason,
        detail=detail,
        domain=str(domain or ""),
        args_preview=_preview_args(args),
        timeout_s=timeout_s,
        approval_choices=choices,
    )
    with _lock:
        _pending[request_id] = pending

    payload = _pending_payload(pending)
    event_type = "permission_request"
    try:
        loop = scheduler.get_event_loop()
        if loop is not None:
            bus.publish_threadsafe(loop, channel, event_type, payload)
        else:
            log.warning("no event loop for permission SSE on %s", channel)
    except Exception:  # noqa: BLE001
        log.exception("failed to publish permission_request")

    approved = False
    mode = "once"
    if pending.event.wait(timeout=timeout_s):
        approved = bool(pending.approved)
        mode = str(pending.mode or "once")
        if mode not in {"once", "always"}:
            mode = "once"
    else:
        log.info("permission %s timed out on %s", request_id, channel)
        try:
            loop = scheduler.get_event_loop()
            if loop is not None:
                bus.publish_threadsafe(
                    loop,
                    channel,
                    "permission_resolved",
                    {"request_id": request_id, "approved": False, "timed_out": True},
                )
        except Exception:  # noqa: BLE001
            pass

    with _lock:
        _pending.pop(request_id, None)
    return {"approved": approved, "mode": mode, "domain": str(domain or pending.domain or "")}


def resolve(
    request_id: str,
    approved: bool,
    *,
    mode: str | None = None,
) -> dict[str, Any]:
    with _lock:
        pending = _pending.get(request_id)
    if not pending:
        raise KeyError("permission request not found or already resolved")
    pending.approved = bool(approved)
    if approved:
        chosen = str(mode or "once").strip().lower()
        pending.mode = chosen if chosen in {"once", "always"} else "once"
    else:
        pending.mode = "once"
    pending.event.set()
    try:
        loop = scheduler.get_event_loop()
        if loop is not None:
            bus.publish_threadsafe(
                loop,
                pending.channel,
                "permission_resolved",
                {
                    "request_id": request_id,
                    "approved": bool(approved),
                    "mode": pending.mode,
                    "timed_out": False,
                },
            )
    except Exception:  # noqa: BLE001
        log.exception("failed to publish permission_resolved")
    return {
        "request_id": request_id,
        "approved": bool(approved),
        "mode": pending.mode,
        "tool": pending.tool,
        "domain": pending.domain,
    }


def get_pending(request_id: str) -> dict[str, Any] | None:
    with _lock:
        pending = _pending.get(request_id)
    if not pending:
        return None
    return _pending_payload(pending, include_channel=True)


def list_pending(channel: str) -> list[dict[str, Any]]:
    """Return unresolved permission requests for an SSE channel."""
    with _lock:
        pending = [item for item in _pending.values() if item.channel == channel]
    return [_pending_payload(item) for item in sorted(pending, key=lambda item: item.created_at)]


def _pending_payload(
    pending: PendingPermission,
    *,
    include_channel: bool = False,
) -> dict[str, Any]:
    payload = {
        "request_id": pending.request_id,
        "tool": pending.tool,
        "reason": pending.reason,
        "detail": pending.detail,
        "args_preview": pending.args_preview,
        "timeout_s": int(pending.timeout_s),
        "domain": pending.domain,
        "approval_choices": pending.approval_choices,
        "age_s": int(time.time() - pending.created_at),
    }
    if include_channel:
        payload["channel"] = pending.channel
    return payload


def _preview_args(args: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in list(args.items())[:8]:
        s = str(v)
        out[k] = s if len(s) <= 200 else s[:199] + "…"
    return out
