"""Restricted user Hook command dispatcher."""

from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class DispatchResult:
    decision: str = "none"
    reason: str = ""
    outcomes: tuple[dict[str, Any], ...] = ()


def _payload(handler: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    data = dict(payload)
    if handler.get("payload_profile") == "codex-v1":
        run = data.get("run") or {}
        data.update({"hook_event_name": handler.get("codex_event_name") or data.get("event"), "session_id": run.get("session_id", ""), "turn_id": run.get("run_id", "")})
        for field in ("trafficlight_agent_name", "trafficlight_keepalive"):
            if field in handler:
                data[field] = handler[field]
    return data


def execute_handler(handler: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    started = time.monotonic()
    mode = handler["mode"]
    try:
        completed = subprocess.run(
            [handler["command"], *handler.get("args", [])],
            input=json.dumps(_payload(handler, payload), ensure_ascii=False).encode(),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=float(handler.get("timeout_seconds", 10)),
            env={key: os.environ[key] for key in ("PATH", "HOME", "LANG") if key in os.environ}, check=False,
        )
    except subprocess.TimeoutExpired:
        return {"execution": "timed_out", "effects": [], "control": {"decision": "none"}, "failure_kind": "timeout", "duration_ms": int((time.monotonic() - started) * 1000)}
    except OSError:
        return {"execution": "failed", "effects": [], "control": {"decision": "none"}, "failure_kind": "execution_error", "duration_ms": int((time.monotonic() - started) * 1000)}
    duration = int((time.monotonic() - started) * 1000)
    if completed.returncode != 0:
        return {"execution": "failed", "effects": [], "control": {"decision": "none"}, "failure_kind": "exit_nonzero", "duration_ms": duration}
    if mode == "observe":
        kind = "trafficlight:sent" if handler.get("effect_kind") == "trafficlight" else "command:completed"
        return {"execution": "succeeded", "effects": [{"type": kind}], "control": {"decision": "none"}, "duration_ms": duration}
    line = next((line for line in completed.stdout.decode("utf-8", "replace").splitlines() if line.strip()), "")
    try:
        result = json.loads(line)
        decision = result.get("decision")
        if decision not in {"allow", "deny"}:
            raise ValueError
    except (ValueError, json.JSONDecodeError):
        return {"execution": "failed", "effects": [], "control": {"decision": "none"}, "failure_kind": "invalid_output", "duration_ms": duration}
    return {"execution": "succeeded", "effects": [], "control": {"decision": decision, "reason": str(result.get("reason") or "")[:500]}, "duration_ms": duration}


def _matches(hook: dict[str, Any], payload: dict[str, Any]) -> bool:
    matcher = hook.get("matcher") or {}
    if not matcher:
        return True
    tool = payload.get("tool") or {}
    for field, expected in matcher.items():
        actual = tool.get("name") if field == "tool.name" else payload.get(field)
        values = expected if isinstance(expected, list) else [expected]
        if actual not in values:
            return False
    return True


def dispatch(hooks: list[dict[str, Any]], event: str, payload: dict[str, Any]) -> DispatchResult:
    records: list[dict[str, Any]] = []
    controllable = event in {"UserPromptSubmit", "PreToolUse"}
    for hook in hooks:
        if not hook.get("enabled") or hook.get("event") != event or not _matches(hook, payload):
            continue
        outcome = execute_handler(hook["handler"], {**payload, "event": event, "event_id": payload.get("event_id") or f"hook_evt_{uuid.uuid4().hex}"})
        decision = outcome["control"]["decision"]
        effective = controllable and decision == "deny"
        if outcome["execution"] in {"failed", "timed_out"} and hook.get("failure_policy") == "deny" and controllable:
            decision, effective = "deny", True
            outcome["control"] = {"decision": "deny", "reason": "Hook failed closed"}
        outcome.update({"hook_id": hook["hook_id"], "event": event, "effective": effective, "decision_ignored": decision == "deny" and not controllable})
        records.append(outcome)
        if effective:
            return DispatchResult("deny", outcome["control"].get("reason", ""), tuple(records))
    return DispatchResult(outcomes=tuple(records))
