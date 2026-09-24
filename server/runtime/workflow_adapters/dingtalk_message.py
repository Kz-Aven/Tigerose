"""Durable adapter for one DingTalk mutation: sending a group message."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from server.connectors import dingtalk_cli
from server.connectors.dingtalk_operations import describe_dingtalk_command, receipt_from_output
from server.runtime.workflow_runtime import ExecutionResult


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _intent(attempt: dict[str, Any]) -> dict[str, Any]:
    return dict(attempt.get("dispatch") or {})


def _validated_send(intent: dict[str, Any]) -> tuple[list[str], str] | None:
    argv = intent.get("argv")
    target = str(intent.get("target_id") or "")
    if not isinstance(argv, list) or any(not isinstance(item, str) for item in argv):
        return None
    operation = describe_dingtalk_command(argv)
    if operation.effect != "write" or operation.resource_type != "message" or operation.operation != "send":
        return None
    if not target or operation.target_id != target:
        return None
    expected = str(intent.get("payload_digest") or "")
    if not expected or expected != _digest({"argv": argv, "target_id": target}):
        return None
    return argv, target


def execute(task: dict[str, Any], attempt: dict[str, Any]) -> ExecutionResult:
    intent = _intent(attempt)
    validated = _validated_send(intent)
    if validated is None:
        return ExecutionResult("failed", error_code="invalid_dingtalk_message_intent")
    argv, target = validated
    result = dingtalk_cli.run_cli(argv, timeout_seconds=int(intent.get("timeout_seconds") or 60))
    if result.outcome == "ok":
        receipt = dict(result.metadata.get("acceptance_receipt") or {})
        if not receipt:
            # A write with an unparseable success receipt is not safely replayable.
            return ExecutionResult("unknown", receipt={"target_id": target, "output_digest": _digest(result.content)})
        return ExecutionResult("succeeded", receipt=receipt)
    if result.outcome == "in_doubt":
        return ExecutionResult("unknown", receipt={"target_id": target, "output_digest": _digest(result.content)})
    if result.outcome in {"timeout", "denied"}:
        return ExecutionResult("unknown", receipt={"target_id": target, "outcome": result.outcome})
    return ExecutionResult("failed", error_code=f"dingtalk_{result.outcome}")


def reconcile(task: dict[str, Any], attempt: dict[str, Any]) -> Literal["succeeded", "absent", "unknown"]:
    intent = _intent(attempt)
    query = intent.get("reconcile_argv")
    if not isinstance(query, list) or not query or any(not isinstance(item, str) for item in query):
        return "unknown"
    result = dingtalk_cli.run_cli(query, timeout_seconds=int(intent.get("timeout_seconds") or 60))
    if result.outcome != "ok":
        return "unknown"
    operation = describe_dingtalk_command(query)
    receipt = receipt_from_output(operation, result.content)
    expected_target = str(intent.get("target_id") or "")
    if receipt and receipt.get("target_id") == expected_target:
        return "succeeded"
    # Queries without a parseable matching receipt cannot prove absence for a
    # send, so they deliberately remain human-confirmed rather than replayed.
    return "unknown"
