"""Run scope contract: capability-derived business domains + runtime danger surface."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from server.runtime.task_intent import TaskIntent
from server.runtime.tool_semantics import domains_for_mcp_ids, resolve_tool_semantics

_RUNTIME_EDIT_PREFIXES = (
    "server/runtime/",
    "tests/runtime/",
    ".git/",
)

_BASH_BYPASS_RE = re.compile(
    r"python\s+-c|python3\s+-c|<<-?|<<|>\s|\|\s|&&|\|\||`|\$\(",
    re.I,
)

ACTION_ALLOW = "allow"
ACTION_ASK = "ask"
ACTION_DENY = "deny"

SCOPE_CAPABILITY_REASON = "scope_capability_domain"

INTERNAL_TOOLS = frozenset({
    "bash_status", "bash_wait", "bash_cancel",
    "create_task", "claim_task", "todo_write", "complete_task", "fail_task",
    "cancel_task", "release_task", "send_message", "request_plan",
    "request_shutdown", "keep_worktree", "archive_completed_tasks",
})
EXPLICIT_REQUEST_TOOLS = frozenset({
    "remember", "create_worktree", "schedule_cron", "cancel_cron",
})


@dataclass
class RunScope:
    intent: TaskIntent
    user_request: str = ""
    objective_domain: str = "general"
    allowed_domains: list[str] = field(default_factory=list)
    # Domains granted for the rest of this run via「全部允许」.
    granted_domains: list[str] = field(default_factory=list)
    enabled_mcp_ids: list[str] = field(default_factory=list)
    connector_executables: list[str] = field(default_factory=list)
    forbidden_expansions: list[str] = field(default_factory=list)
    allow_runtime_edit: bool = False
    allow_bash: bool = False
    allow_spawn: bool = False
    max_diagnostics: int = 2

    def effective_domains(self) -> set[str]:
        return set(self.allowed_domains) | set(self.granted_domains)

    def grant_domain(self, domain: str) -> None:
        domain = (domain or "").strip()
        if domain and domain not in self.granted_domains:
            self.granted_domains.append(domain)


def build_scope_for_intent(
    intent: TaskIntent,
    *,
    user_message: str = "",
    domain_hint: str = "",
    enabled_mcp_ids: list[str] | None = None,
    connector_executables: list[str] | None = None,
) -> RunScope:
    """Build run scope.

    Business write allowlist = domains projected from enabled MCP capabilities.
    Message text is not used as a deny/allow authority for business MCP writes.
    """
    mcp_ids = [str(x) for x in (enabled_mcp_ids or []) if str(x).strip()]
    connector_commands = [str(x) for x in (connector_executables or []) if str(x).strip()]
    capability_domains = domains_for_mcp_ids(mcp_ids)
    # Runtime edits never auto-enable from user-message keywords; only HITL grant.
    allow_runtime_edit = False

    primary = (domain_hint or "").strip() or (
        capability_domains[0] if capability_domains else "general"
    )
    # Prefer a concrete business domain when present.
    for preferred in ("dingtalk.message", "dingtalk.calendar"):
        if preferred in capability_domains:
            primary = preferred
            break

    forbidden_business = [
        "runtime.source_edit",
        "runtime.test_edit",
        "session.internal_mutation",
    ]

    if intent == "durable_goal":
        allowed = list(capability_domains)
        for extra in (
            "general",
            "runtime.query",
            "runtime.mutation",
        ):
            if extra not in allowed:
                allowed.append(extra)
        forbidden = ["session.internal_mutation"]
        if not allow_runtime_edit:
            forbidden.extend(["runtime.source_edit", "runtime.test_edit"])
        return RunScope(
            intent=intent,
            user_request=user_message,
            objective_domain=primary,
            allowed_domains=allowed,
            enabled_mcp_ids=mcp_ids,
            connector_executables=connector_commands,
            forbidden_expansions=forbidden,
            allow_runtime_edit=allow_runtime_edit,
            allow_bash=True,
            allow_spawn=True,
            max_diagnostics=8,
        )

    return RunScope(
        intent=intent,
        user_request=user_message,
        objective_domain=primary,
        allowed_domains=list(capability_domains) if capability_domains else [],
        enabled_mcp_ids=mcp_ids,
        connector_executables=connector_commands,
        forbidden_expansions=list(forbidden_business),
        allow_runtime_edit=allow_runtime_edit,
        allow_bash=False,
        allow_spawn=False,
        max_diagnostics=2 if intent == "one_shot_action" else 2,
    )


def _normalize_path(path: str, cwd: str | None) -> str:
    raw = (path or "").strip()
    if not raw:
        return ""
    base = Path(cwd or os.getcwd())
    try:
        resolved = (base / raw).resolve()
    except OSError:
        return raw.replace("\\", "/")
    try:
        return str(resolved.relative_to(base.resolve())).replace("\\", "/")
    except ValueError:
        return str(resolved).replace("\\", "/")


def _is_runtime_path(path: str) -> bool:
    normalized = path.replace("\\", "/").lower()
    for prefix in _RUNTIME_EDIT_PREFIXES:
        if prefix in normalized or normalized.endswith(prefix.rstrip("/")):
            return True
    return False


def _extract_file_path(tool_name: str, args: dict[str, Any] | None) -> str:
    payload = args or {}
    for key in ("path", "file_path", "target_path"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _is_new_empty_file_write(args: dict[str, Any], cwd: str | None) -> bool:
    """Whether write_file only creates a previously absent empty file."""
    if args.get("content") != "":
        return False
    path = _extract_file_path("write_file", args)
    if not path:
        return False
    try:
        target = Path(path)
        if not target.is_absolute():
            target = Path(cwd or os.getcwd()) / target
        return not target.resolve().exists()
    except OSError:
        return False


def _is_connector_cli_command(command: str, executable_names: list[str]) -> bool:
    from server.connectors.runtime import is_managed_cli_command

    return is_managed_cli_command(command, executable_names)


def _domain_allowed(scope: RunScope, domain: str, server_id: str = "") -> bool:
    effective = scope.effective_domains()
    if not domain:
        # No domain → treat enabled server fallback as authority when present.
        if server_id and f"mcp.{server_id}" in effective:
            return True
        return not bool(scope.allowed_domains) and not bool(scope.enabled_mcp_ids)
    if domain in effective:
        return True
    if server_id and f"mcp.{server_id}" in effective:
        return True
    return False


def classify_scope(
    scope: RunScope,
    tool_name: str,
    args: dict[str, Any] | None,
    cwd: str | None = None,
    trusted_roots: tuple[Path, ...] = (),
) -> dict[str, str] | None:
    """Return a permission-compatible gate, or None if the call may proceed.

    Business MCP writes outside capability domains ask (once/always), not deny.
    """
    name = (tool_name or "").strip()
    payload = args if isinstance(args, dict) else {}

    from server.runtime.tools.mesh import MESH_TOOL_SPECS

    if name in INTERNAL_TOOLS or name in MESH_TOOL_SPECS:
        return None

    if name == "review_plan":
        if scope.allow_spawn:
            return None
        return {
            "action": ACTION_ASK,
            "reason": "scope_plan_review",
            "detail": "当前任务尚未授权多人协作，请确认是否审核此计划。",
            "domain": "runtime.plan_review",
        }

    if name in EXPLICIT_REQUEST_TOOLS:
        return {
            "action": ACTION_ASK,
            "reason": "scope_explicit_request",
            "detail": "该操作需要用户明确要求；无法确认请求与本次操作一致时，需要确认。",
            "domain": f"runtime.request.{name}",
        }

    if name in {"task", "spawn"} or name.startswith("spawn_"):
        if not scope.allow_spawn:
            return {
                "action": ACTION_DENY,
                "reason": "scope_spawn",
                "detail": (
                    "当前任务不能创建额外助理，因此没有执行委派。\n"
                    "如需多人协作，请明确要求拆分并委派任务，或进入项目协作流程。"
                ),
            }

    if name == "bash":
        if _is_connector_cli_command(str(payload.get("command") or ""), scope.connector_executables):
            return None
        from server.runtime.command_plan import classify_bash_command

        plan = classify_bash_command(str(payload.get("command") or ""))
        if plan.risk == "verified_read":
            return None
        if plan.risk == "high_risk":
            return {
                "action": ACTION_ASK,
                "reason": "scope_bash_high_risk",
                "detail": "高风险终端命令必须逐项确认，不能复用此前的授权。",
                "domain": "runtime.bash.high_risk",
            }
        if plan.risk == "mutation":
            return {
                "action": ACTION_ASK,
                "reason": "scope_bash_mutation",
                "detail": "命令计划包含工作区写入操作。可确认本次计划，或授权当前任务内同类写入。",
                "domain": plan.grant_group,
                "approval_choices": "once,always",
            }
        return {
            "action": ACTION_ASK,
            "reason": "scope_bash_unknown",
            "detail": "系统无法证明该命令只读且只访问当前工作区。请确认后执行。",
            "domain": plan.grant_group,
            "approval_choices": "once,always",
        }

    if name in {"edit_file", "write_file", "excel_write"}:
        from server.runtime.tools.files import is_under_roots

        path = _extract_file_path(name, payload)
        if path:
            candidate = Path(path)
            if not candidate.is_absolute():
                candidate = Path(cwd or os.getcwd()) / candidate
            if is_under_roots(candidate.resolve(), trusted_roots):
                return None
        if name == "write_file" and _is_new_empty_file_write(payload, cwd):
            return None
        rel_path = _extract_file_path(name, payload)
        abs_path = _normalize_path(rel_path, cwd)
        if _is_runtime_path(abs_path) and not scope.allow_runtime_edit:
            return {
                "action": ACTION_ASK,
                "reason": "scope_runtime_edit",
                "detail": (
                    "当前任务默认禁止修改 Runtime / 测试源码"
                    f"（目标路径：{rel_path or abs_path}）。\n"
                    "这通常意味着助手想把业务任务扩张成修代码。\n"
                    "请选择「允许一次」仅放行本次编辑，或「全部允许」放行本轮后续 Runtime 编辑。"
                ),
                "domain": "runtime.source_edit",
                "approval_choices": "once,always",
            }
        if name in {"write_file", "excel_write"}:
            return None
        return {
            "action": ACTION_ASK,
            "reason": "scope_file_edit",
            "detail": f"助理请求修改已有文件：{rel_path or abs_path}。请确认本次编辑。",
            "domain": "runtime.file.edit",
            "approval_choices": "once",
        }

    sem = resolve_tool_semantics(name, payload)
    if sem.domain and sem.domain.startswith("runtime."):
        if sem.effect == "write" and not scope.allow_runtime_edit:
            return {
                "action": ACTION_ASK,
                "reason": "scope_runtime_domain",
                "detail": (
                    f"工具域 {sem.domain!r} 超出当前业务 run 范围。\n"
                    "请选择「允许一次」或「全部允许」放行 Runtime 相关写入。"
                ),
                "domain": sem.domain,
                "approval_choices": "once,always",
            }

    is_write = sem.effect == "write" or sem.goal_role in {
        "terminal_action",
        "supporting_mutation",
    }
    if is_write and sem.domain and not _domain_allowed(scope, sem.domain, sem.server_id):
        enabled = scope.enabled_mcp_ids or []
        return {
            "action": ACTION_ASK,
            "reason": SCOPE_CAPABILITY_REASON,
            "detail": (
                f"工具域 {sem.domain!r} 不在当前助理已勾选 MCP 的允许域内。\n"
                f"当前允许域：{sorted(scope.effective_domains())!r}\n"
                f"已勾选 MCP：{enabled!r}\n"
                "请选择「允许一次」仅放行本次调用，或「全部允许」放行本轮该域后续写入。"
            ),
            "domain": sem.domain,
            "server_id": sem.server_id or "",
            "approval_choices": "once,always",
        }

    if sem.effect == "unknown" and name.startswith("mcp__"):
        if sem.server_id and _domain_allowed(scope, sem.domain, sem.server_id):
            return None
        return {
            "action": ACTION_ASK,
            "reason": "scope_unknown_mcp",
            "detail": (
                f"未知 MCP 工具语义：{name}。\n"
                "默认不自动执行；可用「允许一次」或「全部允许」放行。"
            ),
            "domain": sem.domain or (f"mcp.{sem.server_id}" if sem.server_id else ""),
            "server_id": sem.server_id or "",
            "approval_choices": "once,always",
        }

    return None


def check_tool_in_scope(
    scope: RunScope,
    tool_name: str,
    args: dict[str, Any] | None,
    cwd: str | None = None,
) -> tuple[bool, str]:
    """Backward-compatible boolean check.

    Hard denies return (False, reason). Ask-gated calls return (True, "") so
    legacy callers do not hard-block; the tool loop should use classify_scope().
    """
    gate = classify_scope(scope, tool_name, args, cwd)
    if gate is None:
        return True, ""
    if gate.get("action") == ACTION_DENY:
        return False, str(gate.get("detail") or gate.get("reason") or "scope denied")
    return True, ""
