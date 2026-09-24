"""Objective acceptance checks for Goal / task completion."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import yaml

from avent_paths import user_plugins_dir
from server.runtime.attempt_ledger import AttemptState
from server.runtime.objectives import Objective, TaskIntent

_INSTALL_HINT = re.compile(
    r"(安装|install|注册|register).{0,40}(插件|plugin)|"
    r"(插件|plugin).{0,40}(安装|install|注册|register)|"
    r"backend-architect|software-architect",
    re.IGNORECASE,
)

_PLUGIN_NAME = re.compile(
    r"(?:plugins[/\\]|plugin:plugins/)?([a-zA-Z0-9][a-zA-Z0-9._-]{1,80})",
    re.IGNORECASE,
)


def looks_like_plugin_install(text: str) -> bool:
    return bool(_INSTALL_HINT.search(text or ""))


def extract_plugin_names(*texts: str) -> list[str]:
    found: list[str] = []
    blob = "\n".join(t for t in texts if t)
    for match in re.finditer(
        r"(?:plugins[/\\]|plugin:plugins/)([a-zA-Z0-9][a-zA-Z0-9._-]{1,80})",
        blob,
        flags=re.IGNORECASE,
    ):
        name = match.group(1)
        if name not in found:
            found.append(name)
    # Common bare names when install intent is clear
    if looks_like_plugin_install(blob):
        for bare in ("backend-architect", "software-architect", "data-analytics-reporter"):
            if bare in blob and bare not in found:
                found.append(bare)
    return found


def plugin_installed(name: str) -> dict[str, Any]:
    """Hard acceptance: plugin present under user_plugins_dir with valid plugin.yaml."""
    clean = (name or "").strip().removeprefix("plugin:").removeprefix("plugins/")
    clean = clean.strip("/").split("/")[0]
    root = user_plugins_dir() / clean
    manifest = root / "plugin.yaml"
    ok = root.is_dir() and manifest.is_file()
    detail = ""
    if ok:
        try:
            data = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
            if not isinstance(data, dict) or not data.get("name"):
                ok = False
                detail = "plugin.yaml missing name"
        except Exception as exc:  # noqa: BLE001
            ok = False
            detail = f"plugin.yaml unreadable: {exc}"
    else:
        detail = f"missing under {user_plugins_dir()}: {clean}"
    # Registry optional soft check
    registry_ok = False
    try:
        from server.db import repos

        for row in repos.list_capability_registry():
            cid = str(row.get("capability_id") or "")
            if clean in cid or cid.endswith(f"/{clean}"):
                registry_ok = True
                break
    except Exception:  # noqa: BLE001
        registry_ok = False
    return {
        "ok": ok,
        "name": clean,
        "path": str(root),
        "registry_ok": registry_ok,
        "detail": detail,
    }


def assert_plugin_install_for_texts(*texts: str) -> str | None:
    """Return denial message if install intent present but plugins missing; else None."""
    if not any(looks_like_plugin_install(t or "") for t in texts):
        return None
    names = extract_plugin_names(*texts)
    if not names:
        return (
            "Plugin install goal requires objective evidence under "
            f"{user_plugins_dir()}, but no plugin name could be inferred."
        )
    missing = []
    for name in names:
        result = plugin_installed(name)
        if not result["ok"]:
            missing.append(f"{name} ({result['detail']})")
    if missing:
        return (
            "Plugin install acceptance failed — not present in system plugin dir "
            f"({user_plugins_dir()}): " + "; ".join(missing)
        )
    return None


def path_is_under_user_plugins(path: str | Path) -> bool:
    try:
        target = Path(path).expanduser().resolve()
        root = user_plugins_dir().resolve()
        target.relative_to(root)
        return True
    except Exception:  # noqa: BLE001
        return False


@dataclass
class AcceptanceDecision:
    status: str
    objective_id: str
    reason: str
    receipt_ids: list[str] = field(default_factory=list)
    superseded_attempt_ids: list[str] = field(default_factory=list)


@runtime_checkable
class DomainAcceptor(Protocol):
    acceptance_type: str

    def accepts(
        self,
        *,
        intent: TaskIntent,
        objective: Objective,
        attempts: list[AttemptState],
        final_claim: str,
    ) -> AcceptanceDecision: ...


def _base_decision(objective: Objective, status: str, reason: str) -> AcceptanceDecision:
    return AcceptanceDecision(
        status=status,
        objective_id=objective.objective_id,
        reason=reason,
    )


def _iter_terminal_attempts(attempts: list[AttemptState]) -> list[AttemptState]:
    return [
        a
        for a in attempts
        if a.goal_role == "terminal_action" and a.status in {"succeeded", "reconciled_succeeded"}
    ]


class MessageSentAcceptor:
    acceptance_type = "message_sent"

    def accepts(
        self,
        *,
        intent: TaskIntent,
        objective: Objective,
        attempts: list[AttemptState],
        final_claim: str,
    ) -> AcceptanceDecision:
        if not objective.resolved_target_id:
            return _base_decision(objective, "insufficient", "target not resolved")

        for attempt in _iter_terminal_attempts(attempts):
            receipt = attempt.acceptance_receipt or {}
            receipt_id = str(receipt.get("receipt_id") or "")
            target_id = str(receipt.get("target_id") or "")
            if not receipt_id or not target_id:
                continue

            if target_id != objective.resolved_target_id:
                return _base_decision(
                    objective,
                    "rejected",
                    f"receipt target {target_id} does not match objective target {objective.resolved_target_id}",
                )

            if not objective.expected_payload_digest or not attempt.attempted_payload_digest:
                continue
            if attempt.attempted_payload_digest != objective.expected_payload_digest:
                continue

            if (
                objective.idempotency_key
                and attempt.idempotency_key
                and objective.idempotency_key != attempt.idempotency_key
            ):
                return _base_decision(objective, "rejected", "idempotency key mismatch")

            superseded = [
                a.attempt_id
                for a in attempts
                if a.objective_id == objective.objective_id
                and a.status in {"failed", "in_doubt", "superseded"}
                and a.attempt_id != attempt.attempt_id
            ]
            return AcceptanceDecision(
                status="accepted",
                objective_id=objective.objective_id,
                reason="message sent receipt matches objective target and payload",
                receipt_ids=[receipt_id],
                superseded_attempt_ids=superseded,
            )

        return _base_decision(objective, "insufficient", "no valid message_sent receipt")


class CalendarCreatedAcceptor:
    acceptance_type = "calendar_created"

    def accepts(
        self,
        *,
        intent: TaskIntent,
        objective: Objective,
        attempts: list[AttemptState],
        final_claim: str,
    ) -> AcceptanceDecision:
        if not objective.resolved_target_id:
            return _base_decision(objective, "insufficient", "target not resolved")

        for attempt in _iter_terminal_attempts(attempts):
            receipt = attempt.acceptance_receipt or attempt.receipt or {}
            event_id = str(receipt.get("event_id") or receipt.get("receipt_id") or "")
            calendar_id = str(receipt.get("calendar_id") or receipt.get("target_id") or "")
            if not event_id:
                continue
            if calendar_id != objective.resolved_target_id:
                return _base_decision(objective, "rejected", "calendar target mismatch")
            if not objective.expected_payload_digest or not attempt.attempted_payload_digest:
                continue
            if attempt.attempted_payload_digest != objective.expected_payload_digest:
                continue
            return AcceptanceDecision(
                status="accepted",
                objective_id=objective.objective_id,
                reason="calendar event created with matching payload",
                receipt_ids=[event_id],
            )
        return _base_decision(objective, "insufficient", "no valid calendar_created receipt")


class CalendarDeletedAcceptor:
    acceptance_type = "calendar_deleted"

    def accepts(
        self,
        *,
        intent: TaskIntent,
        objective: Objective,
        attempts: list[AttemptState],
        final_claim: str,
    ) -> AcceptanceDecision:
        for attempt in _iter_terminal_attempts(attempts):
            receipt = attempt.acceptance_receipt or attempt.receipt or {}
            event_id = str(receipt.get("event_id") or receipt.get("receipt_id") or "")
            if not event_id:
                continue
            if not objective.expected_payload_digest or not attempt.attempted_payload_digest:
                continue
            if attempt.attempted_payload_digest != objective.expected_payload_digest:
                continue
            return AcceptanceDecision(
                status="accepted",
                objective_id=objective.objective_id,
                reason="calendar event deleted",
                receipt_ids=[event_id],
            )
        return _base_decision(objective, "insufficient", "no valid calendar_deleted receipt")


class DingTalkOperationAcceptor:
    """Accept a CLI terminal write only when its receipt matches the frozen intent."""

    acceptance_type = "dingtalk_operation"

    def accepts(
        self,
        *,
        intent: TaskIntent,
        objective: Objective,
        attempts: list[AttemptState],
        final_claim: str,
    ) -> AcceptanceDecision:
        for attempt in _iter_terminal_attempts(attempts):
            receipt = attempt.acceptance_receipt or attempt.receipt or {}
            receipt_id = str(receipt.get("resource_id") or receipt.get("receipt_id") or "")
            if not receipt_id:
                continue
            if str(receipt.get("target_id") or "") != str(objective.resolved_target_id or ""):
                continue
            if not objective.expected_payload_digest or attempt.attempted_payload_digest != objective.expected_payload_digest:
                continue
            return AcceptanceDecision(
                status="accepted",
                objective_id=objective.objective_id,
                reason="DingTalk operation receipt matches the requested resource and state",
                receipt_ids=[receipt_id],
            )
        return _base_decision(objective, "insufficient", "no valid DingTalk operation receipt")


class FileWrittenAcceptor:
    acceptance_type = "file_written"

    def accepts(
        self,
        *,
        intent: TaskIntent,
        objective: Objective,
        attempts: list[AttemptState],
        final_claim: str,
    ) -> AcceptanceDecision:
        if not objective.resolved_target_id:
            return _base_decision(objective, "insufficient", "target not resolved")

        for attempt in _iter_terminal_attempts(attempts):
            receipt = attempt.acceptance_receipt or attempt.receipt or {}
            path = str(receipt.get("path") or receipt.get("target_id") or "")
            if not path:
                continue
            if path != objective.resolved_target_id:
                return _base_decision(objective, "rejected", "file path mismatch")
            if not objective.expected_payload_digest or not attempt.attempted_payload_digest:
                continue
            if attempt.attempted_payload_digest != objective.expected_payload_digest:
                continue
            return AcceptanceDecision(
                status="accepted",
                objective_id=objective.objective_id,
                reason="file written at expected path",
                receipt_ids=[path],
            )
        return _base_decision(objective, "insufficient", "no valid file_written receipt")


ACCEPTORS: dict[str, DomainAcceptor] = {
    "message_sent": MessageSentAcceptor(),
    "calendar_created": CalendarCreatedAcceptor(),
    "calendar_deleted": CalendarDeletedAcceptor(),
    "dingtalk_operation": DingTalkOperationAcceptor(),
    "file_written": FileWrittenAcceptor(),
}


def evaluate_acceptance(
    acceptance_type: str,
    *,
    intent: TaskIntent,
    objective: Objective,
    attempts: list[AttemptState],
    final_claim: str = "",
) -> AcceptanceDecision:
    acceptor = ACCEPTORS.get(acceptance_type)
    if acceptor is None:
        return _base_decision(
            objective,
            "insufficient",
            f"unknown acceptance_type: {acceptance_type}",
        )
    return acceptor.accepts(
        intent=intent,
        objective=objective,
        attempts=attempts,
        final_claim=final_claim,
    )
