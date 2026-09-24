"""Tool semantic registry for effect, goal role, and domain metadata."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

from server.runtime.tools.registry import CONTROL_TOOLS, QUERY_TOOLS

ToolEffect = Literal["read", "write", "control", "unknown"]
GoalRole = Literal[
    "query",
    "supporting_mutation",
    "terminal_action",
    "control",
    "unknown",
]

_QUERY_VERBS = (
    "get",
    "list",
    "search",
    "find",
    "read",
    "query",
    "check",
    "describe",
)
_MUTATION_VERBS = (
    "send",
    "create",
    "update",
    "delete",
    "add",
    "remove",
    "recall",
    "write",
    "edit",
    "install",
    "deploy",
    "set",
    "patch",
)

SERVER_ID_ALIASES: dict[str, str] = {
    "dingtalk_robot_message": "dingtalk_robot_message",
    "机器人消息": "dingtalk_robot_message",
    "钉钉机器人消息": "dingtalk_robot_message",
    "dingtalk_calendar": "dingtalk_calendar",
    "钉钉日历": "dingtalk_calendar",
    "dingtalk": "dingtalk_robot_message",
}

MCP_TOOL_OVERRIDES: dict[tuple[str, str], dict[str, Any]] = {
    ("dingtalk_robot_message", "search_groups_by_keyword"): {
        "effect": "read",
        "goal_role": "query",
        "domain": "dingtalk.group.query",
        "operation": "search",
        "acceptance_type": "",
        "resource_fields": [],
        "retry_policy": "none",
    },
    ("dingtalk_robot_message", "search_my_robots"): {
        "effect": "read",
        "goal_role": "query",
        "domain": "dingtalk.robot.query",
        "operation": "search",
        "acceptance_type": "",
        "resource_fields": [],
        "retry_policy": "none",
    },
    ("dingtalk_robot_message", "list_group_bots"): {
        "effect": "read",
        "goal_role": "query",
        "domain": "dingtalk.robot.query",
        "operation": "list",
        "acceptance_type": "",
        "resource_fields": [],
        "retry_policy": "none",
    },
    ("dingtalk_robot_message", "send_robot_group_message"): {
        "effect": "write",
        "goal_role": "terminal_action",
        "domain": "dingtalk.message",
        "operation": "send",
        "acceptance_type": "message_sent",
        "resource_fields": ["openConversationId"],
        "retry_policy": "idempotency_required",
    },
    ("dingtalk_robot_message", "send_message_by_custom_robot"): {
        "effect": "write",
        "goal_role": "terminal_action",
        "domain": "dingtalk.message",
        "operation": "send",
        "acceptance_type": "message_sent",
        "resource_fields": ["openConversationId"],
        "retry_policy": "idempotency_required",
    },
    ("dingtalk_robot_message", "recall_robot_group_message"): {
        "effect": "write",
        "goal_role": "terminal_action",
        "domain": "dingtalk.message",
        "operation": "recall",
        "acceptance_type": "message_recalled",
        "resource_fields": ["openConversationId"],
        "retry_policy": "idempotency_required",
    },
    ("dingtalk_robot_message", "batch_recall_robot_users_msg"): {
        "effect": "write",
        "goal_role": "terminal_action",
        "domain": "dingtalk.message",
        "operation": "recall",
        "acceptance_type": "message_recalled",
        "resource_fields": [],
        "retry_policy": "idempotency_required",
    },
    ("dingtalk_calendar", "list_calendar_events"): {
        "effect": "read",
        "goal_role": "query",
        "domain": "dingtalk.calendar",
        "operation": "list",
        "acceptance_type": "",
        "resource_fields": [],
        "retry_policy": "none",
    },
    ("dingtalk_calendar", "create_calendar_event"): {
        "effect": "write",
        "goal_role": "terminal_action",
        "domain": "dingtalk.calendar",
        "operation": "create",
        "acceptance_type": "calendar_created",
        "resource_fields": ["calendarId"],
        "retry_policy": "idempotency_required",
    },
    ("dingtalk_calendar", "delete_calendar_event"): {
        "effect": "write",
        "goal_role": "terminal_action",
        "domain": "dingtalk.calendar",
        "operation": "delete",
        "acceptance_type": "calendar_deleted",
        "resource_fields": ["eventId"],
        "retry_policy": "idempotency_required",
    },
}


@dataclass
class ToolSemantics:
    effect: ToolEffect
    goal_role: GoalRole
    domain: str = ""
    operation: str = ""
    acceptance_type: str = ""
    resource_fields: list[str] = field(default_factory=list)
    retry_policy: str = "none"
    server_id: str = ""
    tool_name: str = ""


def normalize_server_id(server_id: str | None) -> str:
    raw = (server_id or "").strip()
    if not raw:
        return ""
    return SERVER_ID_ALIASES.get(raw, raw)


def infer_goal_role_from_name(tool_name: str) -> GoalRole:
    name = (tool_name or "").strip().lower()
    if not name:
        return "unknown"
    segments = re.split(r"[_\-.]+", name)
    for segment in segments:
        if segment in _QUERY_VERBS:
            return "query"
    for segment in segments:
        if segment in _MUTATION_VERBS:
            return "supporting_mutation"
    if any(name.startswith(prefix) for prefix in _QUERY_VERBS):
        return "query"
    if any(name.startswith(prefix) for prefix in _MUTATION_VERBS):
        return "supporting_mutation"
    return "unknown"


def _parse_mcp_wire_name(name: str) -> tuple[str, str]:
    """Split mcp__{server}__{tool}, recovering Chinese-normalized server ids."""
    if not name.startswith("mcp__"):
        return "", name
    # Prefer matching known alias normalizations (e.g. 机器人消息 -> ____).
    try:
        from server.runtime.mcp import normalize_mcp_name
    except Exception:  # pragma: no cover
        normalize_mcp_name = None  # type: ignore[assignment]

    rest = name[len("mcp__") :]
    for display, canonical in SERVER_ID_ALIASES.items():
        if normalize_mcp_name is not None:
            prefix = f"{normalize_mcp_name(display)}__"
        else:
            prefix = f"{display}__"
        if rest.startswith(prefix):
            return canonical, rest[len(prefix) :]
        # Also match already-canonical server ids.
        canon_prefix = f"{canonical}__"
        if rest.startswith(canon_prefix):
            return canonical, rest[len(canon_prefix) :]

    body = name[5:]
    if not body:
        return "", ""
    parts = body.split("__")
    if len(parts) == 1:
        return normalize_server_id(parts[0]), ""
    # Fallback: last segment is tool; join middle as server (handles empty segments).
    tool_name = parts[-1]
    server_raw = "__".join(parts[:-1]).strip("_") or parts[0]
    # Tool-only override match when server collapses to underscores.
    if not server_raw or set(server_raw) <= {"_"}:
        for (sid, tname), _meta in MCP_TOOL_OVERRIDES.items():
            if tname == tool_name:
                return sid, tool_name
    return normalize_server_id(server_raw), tool_name


def _lookup_mcp_override(server_id: str, tool_name: str) -> dict[str, Any] | None:
    canonical = normalize_server_id(server_id)
    override = MCP_TOOL_OVERRIDES.get((canonical, tool_name))
    if override:
        return override
    for (sid, tname), meta in MCP_TOOL_OVERRIDES.items():
        if sid == canonical and tname == tool_name:
            return meta
    return None


def dingtalk_cli_args(name: str, args: dict[str, Any] | None) -> list[str] | None:
    payload = args or {}
    if name == "dingtalk_cli":
        values = payload.get("args")
        return [str(value) for value in values] if isinstance(values, list) else None
    if name != "bash":
        return None
    command = payload.get("command")
    if not isinstance(command, str):
        return None
    try:
        import shlex

        argv = shlex.split(command, posix=True)
    except ValueError:
        return None
    return argv[1:] if argv and argv[0] == "dws" else None


def _builtin_semantics(name: str, args: dict[str, Any] | None) -> ToolSemantics:
    cli_args = dingtalk_cli_args(name, args)
    if cli_args is not None:
        from server.connectors.dingtalk_operations import describe_dingtalk_command

        operation = describe_dingtalk_command(cli_args)
        return ToolSemantics(
            effect=operation.effect,
            goal_role=operation.goal_role,  # type: ignore[arg-type]
            domain=operation.domain,
            operation=operation.operation,
            acceptance_type=operation.acceptance_type,
            resource_fields=["target_id"] if operation.target_id else [],
            retry_policy="verify_before_retry" if operation.effect != "read" else "none",
            tool_name=name,
        )
    if name in CONTROL_TOOLS:
        return ToolSemantics(
            effect="control",
            goal_role="control",
            domain="runtime.control",
            operation=name,
            tool_name=name,
        )
    if name in QUERY_TOOLS:
        return ToolSemantics(
            effect="read",
            goal_role="query",
            domain="runtime.query",
            operation=name,
            tool_name=name,
        )
    if name == "bash":
        from server.runtime.tools.registry import bash_is_query

        command = str((args or {}).get("command") or "")
        if bash_is_query(command):
            return ToolSemantics(
                effect="read",
                goal_role="query",
                domain="runtime.query",
                operation="bash",
                tool_name=name,
            )
        return ToolSemantics(
            effect="write",
            goal_role="supporting_mutation",
            domain="runtime.mutation",
            operation="bash",
            tool_name=name,
        )
    if name in {"edit_file", "write_file"}:
        return ToolSemantics(
            effect="write",
            goal_role="supporting_mutation",
            domain="runtime.file",
            operation=name,
            tool_name=name,
        )
    return ToolSemantics(
        effect="write",
        goal_role="supporting_mutation",
        domain="runtime.mutation",
        operation=name,
        tool_name=name,
    )


def resolve_tool_semantics(
    name: str,
    args: dict[str, Any] | None = None,
    *,
    server_id: str | None = None,
) -> ToolSemantics:
    tool_name = name
    resolved_server = normalize_server_id(server_id)

    if name.startswith("mcp__"):
        resolved_server, tool_name = _parse_mcp_wire_name(name)
    elif resolved_server:
        tool_name = name

    if resolved_server:
        override = _lookup_mcp_override(resolved_server, tool_name)
        if override:
            return ToolSemantics(
                effect=override["effect"],  # type: ignore[arg-type]
                goal_role=override["goal_role"],  # type: ignore[arg-type]
                domain=str(override.get("domain") or ""),
                operation=str(override.get("operation") or tool_name),
                acceptance_type=str(override.get("acceptance_type") or ""),
                resource_fields=list(override.get("resource_fields") or []),
                retry_policy=str(override.get("retry_policy") or "none"),
                server_id=resolved_server,
                tool_name=tool_name,
            )
        goal_role = infer_goal_role_from_name(tool_name)
        effect: ToolEffect = "read" if goal_role == "query" else "unknown"
        return ToolSemantics(
            effect=effect,
            goal_role=goal_role,
            domain=f"mcp.{resolved_server}",
            operation=tool_name,
            server_id=resolved_server,
            tool_name=tool_name,
        )

    return _builtin_semantics(name, args)


def legacy_tool_kind(sem: ToolSemantics) -> str:
    if sem.goal_role == "query":
        return "query"
    if sem.goal_role == "control":
        return "control"
    if sem.goal_role in {"terminal_action", "supporting_mutation"}:
        return "mutation"
    return "mutation"


def domains_for_server(server_id: str | None) -> set[str]:
    """Domains exposed by one MCP server via the semantics registry."""
    canonical = normalize_server_id(server_id)
    if not canonical:
        return set()
    domains: set[str] = {f"mcp.{canonical}"}
    for (sid, _tname), meta in MCP_TOOL_OVERRIDES.items():
        if normalize_server_id(sid) != canonical:
            continue
        domain = str(meta.get("domain") or "").strip()
        if domain:
            domains.add(domain)
    return domains


def domains_for_mcp_ids(mcp_ids: list[str] | None) -> list[str]:
    """Expand assistant mcp capability ids into a stable allowed-domain list."""
    ordered: list[str] = []
    seen: set[str] = set()
    for raw in mcp_ids or []:
        token = str(raw or "").strip()
        if not token:
            continue
        if token.startswith("mcp:"):
            token = token[4:]
        canonical = normalize_server_id(token) or token
        for domain in sorted(domains_for_server(canonical)):
            if domain not in seen:
                seen.add(domain)
                ordered.append(domain)
    return ordered
