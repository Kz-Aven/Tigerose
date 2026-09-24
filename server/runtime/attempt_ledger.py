"""Attempt evidence ledger: session snapshot + per-run JSONL audit stream."""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

_LEDGER_SCHEMA_VERSION = 2

_BLOCKING_STATUSES = frozenset({"failed", "in_doubt"})
_BLOCKING_GOAL_ROLES = frozenset({"supporting_mutation", "terminal_action"})


@dataclass
class AttemptState:
    attempt_id: str
    objective_id: str
    sequence: int
    status: str
    tool_name: str
    effect: str
    goal_role: str
    resolved_target_id: str
    attempted_payload_digest: str
    parent_attempt_id: str | None
    receipt: dict[str, Any] | None = None
    objective_revision: int = 1
    idempotency_key: str = ""
    acceptance_receipt: dict[str, Any] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AttemptState:
        return cls(
            attempt_id=str(data["attempt_id"]),
            objective_id=str(data["objective_id"]),
            sequence=int(data.get("sequence", 0)),
            status=str(data.get("status", "created")),
            tool_name=str(data.get("tool_name", "")),
            effect=str(data.get("effect", "unknown")),
            goal_role=str(data.get("goal_role", "unknown")),
            resolved_target_id=str(data.get("resolved_target_id", "")),
            attempted_payload_digest=str(data.get("attempted_payload_digest", "")),
            parent_attempt_id=data.get("parent_attempt_id"),
            receipt=data.get("receipt"),
            objective_revision=int(data.get("objective_revision", 1)),
            idempotency_key=str(data.get("idempotency_key", "")),
            acceptance_receipt=data.get("acceptance_receipt"),
            metadata=dict(data.get("metadata") or {}),
        )


def _new_attempt_id() -> str:
    return f"attempt_{uuid.uuid4().hex[:12]}"


def _new_event_id() -> str:
    return f"evt_{uuid.uuid4().hex[:12]}"


def evidence_preview(attempts: dict[str, AttemptState] | list[AttemptState], *, limit: int = 50) -> list[dict[str, Any]]:
    """Return up to `limit` recent attempts for UI only — not authoritative."""
    items = list(attempts.values()) if isinstance(attempts, dict) else list(attempts)
    items.sort(key=lambda a: a.sequence, reverse=True)
    return [a.to_dict() for a in items[:limit]]


class AttemptLedger:
    """Session-scoped execution ledger with per-run JSONL append stream."""

    def __init__(
        self,
        *,
        objectives: dict[str, dict[str, Any]] | None = None,
        attempts: dict[str, dict[str, Any]] | None = None,
        run_cursors: dict[str, dict[str, Any]] | None = None,
        schema_version: int = _LEDGER_SCHEMA_VERSION,
    ) -> None:
        self.schema_version = schema_version
        self.objectives: dict[str, dict[str, Any]] = dict(objectives or {})
        self.attempts: dict[str, dict[str, Any]] = dict(attempts or {})
        self.run_cursors: dict[str, dict[str, Any]] = dict(run_cursors or {})

    @classmethod
    def from_context(cls, context: dict[str, Any]) -> AttemptLedger:
        ledger = context.get("execution_ledger") or {}
        return cls(
            objectives=dict(ledger.get("objectives") or {}),
            attempts=dict(ledger.get("attempts") or {}),
            run_cursors=dict(ledger.get("run_cursors") or {}),
            schema_version=int(ledger.get("schema_version", _LEDGER_SCHEMA_VERSION)),
        )

    def to_context(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "objectives": self.objectives,
            "attempts": self.attempts,
            "run_cursors": self.run_cursors,
        }

    @classmethod
    def ensure_in_session_context(cls, context: dict[str, Any]) -> dict[str, Any]:
        if "execution_ledger" not in context or not isinstance(context["execution_ledger"], dict):
            context["execution_ledger"] = cls().to_context()
        else:
            ledger = context["execution_ledger"]
            ledger.setdefault("schema_version", _LEDGER_SCHEMA_VERSION)
            ledger.setdefault("objectives", {})
            ledger.setdefault("attempts", {})
            ledger.setdefault("run_cursors", {})
        return context

    def _get_attempt(self, attempt_id: str) -> AttemptState:
        raw = self.attempts.get(attempt_id)
        if raw is None:
            raise KeyError(f"unknown attempt_id: {attempt_id}")
        return AttemptState.from_dict(raw)

    def _put_attempt(self, attempt: AttemptState) -> None:
        self.attempts[attempt.attempt_id] = attempt.to_dict()

    def create_attempt(
        self,
        *,
        objective_id: str,
        tool_name: str,
        effect: str,
        goal_role: str,
        resolved_target_id: str = "",
        attempted_payload_digest: str = "",
        parent_attempt_id: str | None = None,
        objective_revision: int = 1,
        idempotency_key: str = "",
        sequence: int = 0,
    ) -> AttemptState:
        attempt = AttemptState(
            attempt_id=_new_attempt_id(),
            objective_id=objective_id,
            sequence=sequence,
            status="created",
            tool_name=tool_name,
            effect=effect,
            goal_role=goal_role,
            resolved_target_id=resolved_target_id,
            attempted_payload_digest=attempted_payload_digest,
            parent_attempt_id=parent_attempt_id,
            objective_revision=objective_revision,
            idempotency_key=idempotency_key,
        )
        self._put_attempt(attempt)
        return attempt

    def mark_dispatching(self, attempt_id: str) -> AttemptState:
        attempt = self._get_attempt(attempt_id)
        attempt.status = "dispatching"
        self._put_attempt(attempt)
        return attempt

    def mark_succeeded(
        self,
        attempt_id: str,
        *,
        receipt: dict[str, Any] | None = None,
        acceptance_receipt: dict[str, Any] | None = None,
    ) -> AttemptState:
        attempt = self._get_attempt(attempt_id)
        attempt.status = "succeeded"
        attempt.receipt = receipt
        if acceptance_receipt is not None:
            attempt.acceptance_receipt = acceptance_receipt
        self._put_attempt(attempt)
        return attempt

    def mark_failed(self, attempt_id: str, *, receipt: dict[str, Any] | None = None) -> AttemptState:
        attempt = self._get_attempt(attempt_id)
        attempt.status = "failed"
        attempt.receipt = receipt
        self._put_attempt(attempt)
        return attempt

    def mark_in_doubt(self, attempt_id: str, *, receipt: dict[str, Any] | None = None) -> AttemptState:
        """Record a write whose provider-side effect cannot be ruled out."""
        attempt = self._get_attempt(attempt_id)
        attempt.status = "in_doubt"
        attempt.receipt = receipt
        self._put_attempt(attempt)
        return attempt

    def mark_abandoned(self, attempt_id: str, *, receipt: dict[str, Any] | None = None) -> AttemptState:
        attempt = self._get_attempt(attempt_id)
        attempt.status = "abandoned"
        attempt.receipt = receipt
        self._put_attempt(attempt)
        return attempt

    def supersede(self, old_id: str, new_id: str) -> AttemptState:
        old = self._get_attempt(old_id)
        new = self._get_attempt(new_id)

        if old.objective_id != new.objective_id or old.objective_revision != new.objective_revision:
            raise ValueError("supersede requires same objective_id and revision")
        if old.resolved_target_id != new.resolved_target_id:
            raise ValueError("supersede requires same resolved_target_id")

        objective = self.objectives.get(old.objective_id) or {}
        expected_digest = str(objective.get("expected_payload_digest") or "")
        if expected_digest and new.attempted_payload_digest != expected_digest:
            raise ValueError("supersede requires matching payload digest")

        if old.status not in _BLOCKING_STATUSES:
            raise ValueError(f"cannot supersede attempt in status {old.status}")

        old.status = "superseded"
        self._put_attempt(old)
        return old

    def blocking_attempts(self, objective_id: str | None = None) -> list[AttemptState]:
        """Return open failed/in_doubt supporting_mutation/terminal attempts."""
        results: list[AttemptState] = []
        for raw in self.attempts.values():
            attempt = AttemptState.from_dict(raw)
            if objective_id and attempt.objective_id != objective_id:
                continue
            if attempt.goal_role in {"query", "control"}:
                continue
            if attempt.goal_role not in _BLOCKING_GOAL_ROLES:
                continue
            if attempt.status in _BLOCKING_STATUSES:
                results.append(attempt)
        results.sort(key=lambda a: a.sequence)
        return results

    def append_event(
        self,
        run_id: str,
        event: dict[str, Any],
        runs_dir: Path,
    ) -> dict[str, Any]:
        """Append event to run JSONL with monotonic sequence; update snapshot."""
        runs_dir.mkdir(parents=True, exist_ok=True)
        path = runs_dir / f"{run_id}.jsonl"

        cursor = dict(self.run_cursors.get(run_id) or {})
        next_sequence = int(cursor.get("last_applied_sequence", 0)) + 1

        event_id = str(event.get("event_id") or _new_event_id())
        record = {
            **event,
            "event_id": event_id,
            "sequence": next_sequence,
            "run_id": run_id,
        }

        # Idempotent replay by event_id
        if path.is_file():
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    existing = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if existing.get("event_id") == event_id:
                    return existing

        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

        cursor["last_applied_sequence"] = next_sequence
        self.run_cursors[run_id] = cursor

        attempt_id = event.get("attempt_id")
        if attempt_id and attempt_id in self.attempts:
            attempt = AttemptState.from_dict(self.attempts[attempt_id])
            attempt.sequence = next_sequence
            self._put_attempt(attempt)

        return record
