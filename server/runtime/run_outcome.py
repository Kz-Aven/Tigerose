"""Run outcome mapping from loop termination to persisted status."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from server.runtime.acceptance import AcceptanceDecision

RunOutcomeStatus = Literal[
    "completed",
    "partially_completed",
    "waiting_for_user",
    "blocked_user_action",
    "blocked_runtime",
    "budget_exhausted",
    "failed",
    "cancelled",
]

LoopTerminationReason = Literal[
    "accepted",
    "partial",
    "waiting_for_user",
    "user_block",
    "runtime_block",
    "budget",
    "failed",
    "cancelled",
    "normal_stop",
]


@dataclass
class PendingAction:
    action_id: str
    kind: str
    status: str
    session_id: str
    objective_id: str
    objective_revision: int
    attempt_id: str
    tool_name: str
    frozen_args_digest: str
    continuation_ref: str
    expires_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "kind": self.kind,
            "status": self.status,
            "session_id": self.session_id,
            "objective_id": self.objective_id,
            "objective_revision": self.objective_revision,
            "attempt_id": self.attempt_id,
            "tool_name": self.tool_name,
            "frozen_args_digest": self.frozen_args_digest,
            "continuation_ref": self.continuation_ref,
            "expires_at": self.expires_at,
        }


@dataclass
class RunOutcome:
    status: RunOutcomeStatus
    reason_code: str
    objective_id: str
    acceptance: AcceptanceDecision | None = None
    completed_items: list[str] = field(default_factory=list)
    remaining_items: list[str] = field(default_factory=list)
    resumable: bool = False
    pending_action_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason_code": self.reason_code,
            "objective_id": self.objective_id,
            "acceptance": None if self.acceptance is None else {
                "status": self.acceptance.status,
                "objective_id": self.acceptance.objective_id,
                "reason": self.acceptance.reason,
                "receipt_ids": list(self.acceptance.receipt_ids),
                "superseded_attempt_ids": list(self.acceptance.superseded_attempt_ids),
            },
            "completed_items": list(self.completed_items),
            "remaining_items": list(self.remaining_items),
            "resumable": self.resumable,
            "pending_action_id": self.pending_action_id,
        }


_TERMINATION_TO_OUTCOME: dict[str, RunOutcomeStatus] = {
    "accepted": "completed",
    "partial": "partially_completed",
    "waiting_for_user": "waiting_for_user",
    "user_block": "blocked_user_action",
    "runtime_block": "blocked_runtime",
    "budget": "budget_exhausted",
    "failed": "failed",
    "cancelled": "cancelled",
}


def map_loop_termination(
    *,
    termination_reason: str,
    objective_id: str = "",
    acceptance: AcceptanceDecision | None = None,
    intent: str = "",
    pending_action_id: str | None = None,
    resumable: bool = False,
    reason_code: str = "",
    completed_items: list[str] | None = None,
    remaining_items: list[str] | None = None,
) -> RunOutcome:
    """Map loop termination reason to RunOutcome per design §5.2."""
    completed_items = list(completed_items or [])
    remaining_items = list(remaining_items or [])

    if termination_reason == "normal_stop":
        return RunOutcome(
            status="completed",
            reason_code=reason_code
            or ("acceptance_accepted" if acceptance and acceptance.status == "accepted" else "normal_stop"),
            objective_id=objective_id or (acceptance.objective_id if acceptance else ""),
            acceptance=acceptance,
            completed_items=completed_items,
            remaining_items=remaining_items,
            resumable=False,
        )

    if termination_reason == "accepted":
        if acceptance and acceptance.status == "accepted":
            status: RunOutcomeStatus = "completed"
        elif intent in {"conversation", "read_only"}:
            status = "completed"
        else:
            status = "failed"
        return RunOutcome(
            status=status,
            reason_code=reason_code or termination_reason,
            objective_id=objective_id or (acceptance.objective_id if acceptance else ""),
            acceptance=acceptance,
            completed_items=completed_items,
            remaining_items=remaining_items,
            resumable=False,
        )

    status = _TERMINATION_TO_OUTCOME.get(termination_reason, "failed")
    if termination_reason == "waiting_for_user":
        resumable = bool(pending_action_id)

    return RunOutcome(
        status=status,
        reason_code=reason_code or termination_reason,
        objective_id=objective_id or (acceptance.objective_id if acceptance else ""),
        acceptance=acceptance,
        completed_items=completed_items,
        remaining_items=remaining_items,
        resumable=resumable,
        pending_action_id=pending_action_id,
    )
