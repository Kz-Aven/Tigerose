"""Objective builder and lifecycle helpers for execution ledger."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

TaskIntent = Literal["conversation", "read_only", "one_shot_action", "durable_goal"]

_OBJECTIVE_STATES = frozenset({"draft", "resolved", "frozen", "accepted", "cancelled"})

_BRACKET_TARGET = re.compile(r"【([^】]+)】")
_QUOTED_TARGET = re.compile(r'["「]([^"」]+)["」]')


def _normalize_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def payload_digest(payload: dict[str, Any]) -> str:
    """Return sha256 digest of normalized non-secret payload fields."""
    digest = hashlib.sha256(_normalize_json(payload).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _extract_target_display_name(text: str) -> str | None:
    for pattern in (_BRACKET_TARGET, _QUOTED_TARGET):
        match = pattern.search(text or "")
        if match:
            return match.group(1).strip()
    return None


def _new_objective_id() -> str:
    return f"objective_{uuid.uuid4().hex[:12]}"


@dataclass
class Objective:
    objective_id: str
    intent: TaskIntent
    revision: int
    state: str
    domain: str
    operation: str
    target_ref: dict[str, Any]
    resolved_target_id: str | None
    expected_payload: dict[str, Any]
    expected_payload_digest: str
    idempotency_key: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Objective:
        return cls(
            objective_id=str(data["objective_id"]),
            intent=data["intent"],
            revision=int(data.get("revision", 1)),
            state=str(data.get("state", "draft")),
            domain=str(data.get("domain", "")),
            operation=str(data.get("operation", "")),
            target_ref=dict(data.get("target_ref") or {}),
            resolved_target_id=data.get("resolved_target_id"),
            expected_payload=dict(data.get("expected_payload") or {}),
            expected_payload_digest=str(data.get("expected_payload_digest", "")),
            idempotency_key=str(data.get("idempotency_key", "")),
        )


def build_objective_from_message(
    text: str,
    intent: TaskIntent,
    *,
    domain: str = "",
    operation: str = "",
) -> Objective:
    """Build a draft Objective from user message text."""
    display_name = _extract_target_display_name(text)
    target_ref: dict[str, Any] = {}
    if display_name:
        target_ref["display_name"] = display_name

    # Prefer the second 【】/quote span as message body when present
    # (first is usually the group/target name).
    spans = _BRACKET_TARGET.findall(text or "")
    message_body = spans[1].strip() if len(spans) >= 2 else (text or "").strip()

    expected_payload: dict[str, Any] = {"message_text": message_body}
    if display_name:
        expected_payload["target_display_name"] = display_name

    objective_id = _new_objective_id()
    revision = 1
    digest = payload_digest(expected_payload)

    return Objective(
        objective_id=objective_id,
        intent=intent,
        revision=revision,
        state="draft",
        domain=domain,
        operation=operation,
        target_ref=target_ref,
        resolved_target_id=None,
        expected_payload=expected_payload,
        expected_payload_digest=digest,
        idempotency_key=f"{objective_id}:r{revision}",
    )


def resolve_target(objective: Objective, resolved_target_id: str) -> Objective:
    """Transition draft → resolved with authoritative target id."""
    if objective.state != "draft":
        raise ValueError(f"cannot resolve objective in state {objective.state}")
    if not resolved_target_id:
        raise ValueError("resolved_target_id is required")

    data = objective.to_dict()
    data["state"] = "resolved"
    data["resolved_target_id"] = resolved_target_id
    return Objective.from_dict(data)


def freeze_objective(objective: Objective) -> Objective:
    """Transition to frozen before first write/terminal dispatch."""
    if objective.state not in {"resolved", "draft"}:
        raise ValueError(f"cannot freeze objective in state {objective.state}")
    if objective.state == "draft" and not objective.resolved_target_id:
        raise ValueError("objective must be resolved before freeze")

    data = objective.to_dict()
    data["state"] = "frozen"
    return Objective.from_dict(data)


def canonical_send_payload_digest(
    *,
    message_text: str,
    resolved_target_id: str | None = None,
    target_display_name: str | None = None,
) -> tuple[dict[str, Any], str]:
    """Normalize send payload fields used for expected/attempted digests."""
    payload: dict[str, Any] = {"message_text": (message_text or "").strip()}
    if target_display_name:
        payload["target_display_name"] = target_display_name
    if resolved_target_id:
        payload["resolved_target_id"] = resolved_target_id
    return payload, payload_digest(payload)


def message_text_from_tool_args(args: dict[str, Any] | None) -> str:
    payload = args if isinstance(args, dict) else {}
    for key in ("msg", "message", "text", "content", "markdown"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def extract_target_id_from_tool_result(
    *,
    tool_name: str,
    args: dict[str, Any] | None,
    content: str,
    metadata: dict[str, Any] | None,
) -> str | None:
    """Best-effort extract of authoritative target id from tool args/result."""
    payload = args if isinstance(args, dict) else {}
    from server.runtime.tool_semantics import dingtalk_cli_args

    cli_args = dingtalk_cli_args(tool_name, payload)
    if cli_args is not None:
        from server.connectors.dingtalk_operations import describe_dingtalk_command

        operation = describe_dingtalk_command(cli_args)
        if operation.target_id:
            return operation.target_id
    for key in (
        "openConversationId",
        "open_conversation_id",
        "conversationId",
        "calendarId",
        "eventId",
    ):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    meta = metadata or {}
    receipt = meta.get("acceptance_receipt")
    if isinstance(receipt, dict):
        tid = receipt.get("target_id")
        if isinstance(tid, str) and tid.strip():
            return tid.strip()

    text = content or ""
    for pattern in (
        r'"openConversationId"\s*:\s*"([^"]+)"',
        r"'openConversationId'\s*:\s*'([^']+)'",
        r"openConversationId[=:]\s*([A-Za-z0-9_-]+)",
    ):
        match = re.search(pattern, text)
        if match:
            return match.group(1).strip()
    return None


def operation_payload_digest_for_tool(
    *,
    tool_name: str,
    args: dict[str, Any] | None,
    fallback_payload: dict[str, Any],
) -> str:
    """Use a connector's canonical desired state when it has one."""
    from server.runtime.tool_semantics import dingtalk_cli_args

    cli_args = dingtalk_cli_args(tool_name, args)
    if cli_args is not None:
        from server.connectors.dingtalk_operations import describe_dingtalk_command

        operation = describe_dingtalk_command(cli_args)
        if operation.effect == "write":
            return payload_digest(operation.desired_state)
    return payload_digest(fallback_payload) if fallback_payload else ""


def maybe_resolve_and_freeze(
    objective: Objective,
    *,
    tool_name: str,
    args: dict[str, Any] | None,
    content: str = "",
    metadata: dict[str, Any] | None = None,
    goal_role: str = "",
) -> Objective:
    """Resolve target from query/send args and freeze before terminal writes."""
    payload = args if isinstance(args, dict) else {}
    from server.runtime.tool_semantics import dingtalk_cli_args

    cli_args = dingtalk_cli_args(tool_name, payload)
    current = objective
    target_id = extract_target_id_from_tool_result(
        tool_name=tool_name,
        args=args,
        content=content,
        metadata=metadata,
    )
    if target_id and current.state == "draft":
        current = resolve_target(current, target_id)
    if goal_role == "terminal_action" and current.state in {"draft", "resolved"}:
        if current.resolved_target_id:
            if cli_args is not None:
                from server.connectors.dingtalk_operations import describe_dingtalk_command

                operation = describe_dingtalk_command(cli_args)
                if operation.effect == "write":
                    data = current.to_dict()
                    data["expected_payload"] = operation.desired_state
                    data["expected_payload_digest"] = payload_digest(operation.desired_state)
                    data["state"] = "resolved" if current.state == "draft" else current.state
                    current = freeze_objective(Objective.from_dict(data))
                    return current
            # Align expected digest with the same canonical fields used for attempts.
            display = str((current.target_ref or {}).get("display_name") or "") or None
            message_text = str(
                (current.expected_payload or {}).get("message_text") or ""
            )
            payload, digest = canonical_send_payload_digest(
                message_text=message_text,
                resolved_target_id=current.resolved_target_id,
                target_display_name=display,
            )
            data = current.to_dict()
            data["expected_payload"] = payload
            data["expected_payload_digest"] = digest
            data["state"] = "resolved" if current.state == "draft" else current.state
            current = Objective.from_dict(data)
            current = freeze_objective(current)
    return current
