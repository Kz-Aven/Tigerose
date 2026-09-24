"""DingTalk CLI command descriptions shared by the connector and runtime.

The CLI is intentionally open-ended.  This module recognizes only commands
whose effect and receipt shape we can describe; everything else remains
available but is reported as ``unknown`` instead of being guessed as a write.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

OperationEffect = Literal["read", "write", "unknown"]


@dataclass(frozen=True)
class DingTalkOperation:
    effect: OperationEffect = "unknown"
    goal_role: str = "unknown"
    domain: str = "dingtalk.cli"
    operation: str = "unknown"
    resource_type: str = "command"
    acceptance_type: str = ""
    target_id: str = ""
    argv: tuple[str, ...] = ()

    @property
    def operation_id(self) -> str:
        return f"dingtalk:{self.operation}"

    @property
    def desired_state(self) -> dict[str, Any]:
        flags: dict[str, list[str]] = {}
        positionals: list[str] = []
        index = 0
        args = list(self.argv)
        while index < len(args):
            token = args[index]
            if token.startswith("--"):
                key = token[2:]
                if key in {"format", "jq", "yes", "dry-run", "profile"}:
                    index += 2 if index + 1 < len(args) and not args[index + 1].startswith("-") else 1
                    continue
                value = "true"
                if index + 1 < len(args) and not args[index + 1].startswith("-"):
                    value = args[index + 1]
                    index += 1
                flags.setdefault(key, []).append(value)
            elif not token.startswith("-"):
                positionals.append(token)
            index += 1
        return {
            "connector": "dingtalk",
            "resource_type": self.resource_type,
            "action": self.operation,
            "target_id": self.target_id,
            "arguments": {key: flags[key] for key in sorted(flags)},
            "positionals": positionals,
        }


def _flag(argv: list[str], name: str) -> str:
    try:
        index = argv.index(f"--{name}")
    except ValueError:
        return ""
    return argv[index + 1] if index + 1 < len(argv) else ""


def _operation(
    argv: list[str],
    *,
    effect: OperationEffect,
    goal_role: str,
    domain: str,
    operation: str,
    resource_type: str,
    target_id: str = "",
    accepts: bool = False,
) -> DingTalkOperation:
    return DingTalkOperation(
        effect=effect,
        goal_role=goal_role,
        domain=domain,
        operation=operation,
        resource_type=resource_type,
        acceptance_type="dingtalk_operation" if accepts else "",
        target_id=target_id,
        argv=tuple(argv),
    )


def describe_dingtalk_command(argv: list[str] | None) -> DingTalkOperation:
    args = [str(value) for value in (argv or [])]
    lowered = [value.lower() for value in args]
    if not args:
        return DingTalkOperation(argv=tuple(args))
    if any(value in {"--help", "-h", "help", "schema", "--dry-run"} for value in lowered):
        return _operation(args, effect="read", goal_role="query", domain="dingtalk.cli", operation="describe", resource_type="command")

    root = lowered[0]
    second = lowered[1] if len(lowered) > 1 else ""
    third = lowered[2] if len(lowered) > 2 else ""
    command = " ".join(lowered[:3])

    if root == "calendar":
        target = _flag(args, "calendar-id") or "primary"
        if second in {"+today", "+agenda", "+get", "+search-event", "+book-search"} or command in {"calendar event list", "calendar event get", "calendar attendee list", "calendar participant list"}:
            return _operation(args, effect="read", goal_role="query", domain="dingtalk.calendar", operation="query", resource_type="calendar_event", target_id=target)
        if (second == "event" and third == "create") or second in {"+book", "+create"}:
            return _operation(args, effect="write", goal_role="terminal_action", domain="dingtalk.calendar", operation="create", resource_type="calendar_event", target_id=target, accepts=True)
        if (second == "event" and third == "delete") or second == "+cancel-event":
            return _operation(args, effect="write", goal_role="terminal_action", domain="dingtalk.calendar", operation="delete", resource_type="calendar_event", target_id=_flag(args, "event-id"), accepts=True)
        if second in {"attendee", "participant"} and third in {"add", "delete", "remove"}:
            return _operation(args, effect="write", goal_role="supporting_mutation", domain="dingtalk.calendar", operation=third, resource_type="calendar_attendee", target_id=_flag(args, "event-id"))

    if root == "todo":
        target = _flag(args, "task-id")
        if second.startswith("+") and second in {"+get", "+search", "+list", "+created-todos"} or command in {"todo task get", "todo task list"}:
            return _operation(args, effect="read", goal_role="query", domain="dingtalk.todo", operation="query", resource_type="todo", target_id=target)
        if (second == "task" and third in {"create", "create-sub", "delete", "done", "update"}) or second in {"+create", "+assign", "+delete", "+complete", "+reopen", "+update"}:
            action = third if second == "task" else second[1:]
            return _operation(args, effect="write", goal_role="terminal_action", domain="dingtalk.todo", operation=action, resource_type="todo", target_id=target or "default", accepts=True)

    if root == "chat" and second == "message":
        if third in {"get", "list", "search"}:
            return _operation(args, effect="read", goal_role="query", domain="dingtalk.chat", operation="query", resource_type="message", target_id=_flag(args, "conversation-id"))
        if third in {"send", "recall", "edit", "delete"}:
            return _operation(args, effect="write", goal_role="terminal_action", domain="dingtalk.chat", operation=third, resource_type="message", target_id=_flag(args, "conversation-id"), accepts=True)

    return DingTalkOperation(argv=tuple(args))


def _json_objects(text: str) -> list[dict[str, Any]]:
    try:
        parsed = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return []
    found: list[dict[str, Any]] = []
    stack: list[Any] = [parsed]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            found.append(current)
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)
    return found


def receipt_from_output(operation: DingTalkOperation, output: str) -> dict[str, Any] | None:
    """Extract only authoritative-looking resource identifiers from CLI JSON."""
    keys = {
        "calendar_event": ("eventId", "event_id"),
        "todo": ("taskId", "task_id", "id"),
        "message": ("messageId", "message_id", "processQueryKey", "id"),
    }.get(operation.resource_type, ("id",))
    for item in _json_objects(output):
        resource_id = next((str(item[key]) for key in keys if item.get(key)), "")
        if resource_id:
            return {
                "provider": "dingtalk",
                "resource_id": resource_id,
                "receipt_id": resource_id,
                "event_id": resource_id if operation.resource_type == "calendar_event" else "",
                "calendar_id": operation.target_id if operation.resource_type == "calendar_event" else "",
                "target_id": operation.target_id,
                "resource_type": operation.resource_type,
                "action": operation.operation,
                "operation_id": operation.operation_id,
            }
    return None


def is_in_doubt_failure(operation: DingTalkOperation, output: str) -> bool:
    if operation.effect == "write":
        return True
    # The open CLI surface has no reliable effect declaration. A non-zero
    # result can be a write that reached the provider, so never label it failed.
    if operation.effect != "unknown":
        return False
    normalized = (output or "").lower()
    # These errors originate in dws command/argument validation before a
    # provider request is dispatched, so they are safe ordinary failures.
    local_validation = (
        "unknown command",
        "unknown subcommand",
        "did you mean",
        '"category": "validation"',
        '"category":"validation"',
    )
    return not any(marker in normalized for marker in local_validation)
