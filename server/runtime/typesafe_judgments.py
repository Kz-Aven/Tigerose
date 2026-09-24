"""TypeSafe System One judgments with strict technical-failure signaling."""

from __future__ import annotations

import json
import math
import os
import time
from dataclasses import dataclass
from typing import Any, Callable

from avent_config import cfg_get, get_config


_INTENTS = frozenset({"conversation", "read_only", "one_shot_action", "durable_goal"})
_GOAL_RELATIONSHIPS = frozenset(
    {"goal_control", "goal_continuation", "independent", "goal_switch", "none"}
)
_SENSITIVE_KEY_PARTS = ("api_key", "authorization", "cookie", "password", "secret", "token")


class TypeSafeUnavailable(RuntimeError):
    """A technical failure eligible for the existing LLM fallback path."""

    def __init__(self, kind: str, detail: str = ""):
        self.kind = kind
        self.detail = detail[:200]
        super().__init__(f"typesafe {kind}: {self.detail}".rstrip(": "))


@dataclass(frozen=True)
class TypeSafeDecisionAudit:
    operation: str
    route: str
    choice: str
    confidence: float | None
    noul_probabilities: dict[str, float]
    duration_ms: int
    state_chars: int
    failure_kind: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "typesafe_judgment",
            "operation": self.operation,
            "route": self.route,
            "choice": self.choice,
            "confidence": self.confidence,
            "noul_probabilities": self.noul_probabilities,
            "duration_ms": self.duration_ms,
            "state_chars": self.state_chars,
            "failure_kind": self.failure_kind,
        }


def _setting(name: str, default: float) -> float:
    value = cfg_get(get_config(), "agent", "typesafe", name, default=default)
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _bounded(value: Any, limit: int) -> str:
    return str(value or "")[:limit]


def _safe_number(value: Any, *, question_id: str, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise TypeSafeUnavailable("response_contract", f"{question_id}.{field}") from exc
    if not math.isfinite(number) or number < 0.0 or number > 1.0:
        raise TypeSafeUnavailable("response_contract", f"{question_id}.{field}")
    return number


def _choice(response: Any, question_id: str, allowed: frozenset[str]) -> tuple[str, float]:
    try:
        answer = response.answers[question_id]
        choice = str(answer.choice or "").strip()
    except (AttributeError, KeyError, TypeError) as exc:
        raise TypeSafeUnavailable("response_contract", f"missing choice {question_id}") from exc
    if getattr(answer, "type", "choice") != "choice":
        raise TypeSafeUnavailable("response_contract", f"invalid answer type {question_id}")
    if choice not in allowed:
        raise TypeSafeUnavailable("response_contract", f"invalid choice {question_id}")
    return choice, _safe_number(getattr(answer, "confidence", None), question_id=question_id, field="confidence")


def _noul(response: Any, question_id: str) -> float:
    try:
        answer = response.answers[question_id]
        value = answer.noul
    except (AttributeError, KeyError, TypeError) as exc:
        raise TypeSafeUnavailable("response_contract", f"missing noul {question_id}") from exc
    if getattr(answer, "type", "noul") != "noul":
        raise TypeSafeUnavailable("response_contract", f"invalid answer type {question_id}")
    return _safe_number(value, question_id=question_id, field="noul")


def _failure_kind(exc: Exception) -> str:
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    name = type(exc).__name__.lower()
    detail = str(exc).lower()
    if status == 401 or status == 403 or "authentication" in name or "permission" in name:
        return "auth"
    if status == 429 or "ratelimit" in name or "rate limit" in detail:
        return "rate_limit"
    if isinstance(status, int) and status >= 500:
        return "server_error"
    if "timeout" in name or "timeout" in detail:
        return "timeout"
    if "connection" in name or "connect" in detail or "network" in name:
        return "network"
    return "sdk_unavailable"


def _sdk_client(timeout_s: float) -> tuple[Any, Any, Any]:
    if not (os.environ.get("TYPESAFE_API_KEY") or "").strip():
        raise TypeSafeUnavailable("credentials_missing", "TYPESAFE_API_KEY is not configured")
    try:
        from typesafe_sdk import Choice, Noul, RetryPolicy, TypeSafeClient
    except ImportError as exc:
        raise TypeSafeUnavailable("sdk_unavailable", "typesafe-sdk is not installed") from exc
    try:
        return TypeSafeClient(
            timeout=timeout_s,
            retry=RetryPolicy(max_retries=0),
        ), Choice, Noul
    except Exception as exc:
        raise TypeSafeUnavailable(_failure_kind(exc), type(exc).__name__) from exc


def _system_one(
    *,
    state: dict[str, Any],
    build_questions: Callable[[Any, Any], dict[str, Any]],
    timeout_s: float,
    client_factory: Callable[[float], tuple[Any, Any, Any]] | None,
) -> Any:
    factory = client_factory or _sdk_client
    try:
        client, Choice, Noul = factory(timeout_s)
        return client.system_one(state=state, questions=build_questions(Choice, Noul))
    except TypeSafeUnavailable:
        raise
    except Exception as exc:
        raise TypeSafeUnavailable(_failure_kind(exc), type(exc).__name__) from exc


def _append_audit(audit: TypeSafeDecisionAudit, usage_context: dict[str, Any] | None) -> None:
    run_id = str((usage_context or {}).get("run_id") or "")
    if not run_id:
        return
    try:
        from server.runtime.run_transcript import append_run_event

        append_run_event(run_id, audit.to_dict())
    except Exception:
        pass


def record_typesafe_unavailable(
    operation: str,
    error: TypeSafeUnavailable,
    usage_context: dict[str, Any] | None = None,
) -> None:
    """Record a failed TypeSafe attempt without exposing request content."""
    _append_audit(
        TypeSafeDecisionAudit(
            operation=operation,
            route="fallback_llm",
            choice="",
            confidence=None,
            noul_probabilities={},
            duration_ms=0,
            state_chars=0,
            failure_kind=error.kind,
        ),
        usage_context,
    )


def classify_task_intent_with_typesafe(
    text: str,
    *,
    active_goal_condition: str = "",
    usage_context: dict[str, Any] | None = None,
    client_factory: Callable[[float], tuple[Any, Any, Any]] | None = None,
):
    """Classify one request using closed TypeSafe questions."""
    started = time.monotonic()
    state = {
        "user_request": _bounded(text, 4000),
        "active_goal": (
            {"active": True, "condition": _bounded(active_goal_condition, 500)}
            if active_goal_condition
            else {"active": False}
        ),
    }

    def questions(Choice: Any, Noul: Any) -> dict[str, Any]:
        return {
            "task_intent": Choice(
                instructions="What is the user's primary task shape?",
                criteria={
                    "conversation": "Casual conversation or an answer without substantive work.",
                    "read_only": "Read, explain, inspect, or analyze without changing external state.",
                    "one_shot_action": "One bounded action with one expected result.",
                    "durable_goal": "Multiple independently verifiable work items, dependencies, or iterative delivery.",
                },
            ),
            "goal_relationship": Choice(
                instructions="How does this request relate to the active Goal, if one exists?",
                criteria={
                    "goal_control": "Sets, clears, resumes, or asks about Goal control.",
                    "goal_continuation": "Continues the active Goal.",
                    "independent": "A separate request that must not affect the active Goal.",
                    "goal_switch": "Replaces the active Goal with a different objective.",
                    "none": "No active Goal relationship applies.",
                },
            ),
            "has_independent_verifiable_work": Noul(
                instructions="Does the request contain multiple work items that can be independently verified?"
            ),
            "requires_iterative_delivery": Noul(
                instructions="Does the request require dependencies, iteration, or a final aggregate delivery?"
            ),
        }

    response = _system_one(
        state=state,
        build_questions=questions,
        timeout_s=_setting("intent_timeout_s", 5.0),
        client_factory=client_factory,
    )
    intent, confidence = _choice(response, "task_intent", _INTENTS)
    relationship, _ = _choice(response, "goal_relationship", _GOAL_RELATIONSHIPS)
    independent_work = _noul(response, "has_independent_verifiable_work")
    iterative_delivery = _noul(response, "requires_iterative_delivery")
    signal_threshold = _setting("intent_durable_signal_min_probability", 0.70)
    durable_signals: list[str] = []
    if independent_work >= signal_threshold:
        durable_signals.append("存在可独立验证的多个工作项")
    if iterative_delivery >= signal_threshold:
        durable_signals.append("存在迭代或依赖式交付")

    from server.runtime.task_intent import TaskIntentResult

    result = TaskIntentResult(
        intent=intent,
        confidence=confidence,
        durable_signals=durable_signals,
        reason=(
            f"TypeSafe: {intent}; confidence={confidence:.2f}; "
            f"durable_signals={len(durable_signals)}"
        ),
        goal_relationship=relationship,
        authoritative=True,
        classified=True,
    )
    _append_audit(
        TypeSafeDecisionAudit(
            operation="task_intent",
            route="typesafe",
            choice=intent,
            confidence=confidence,
            noul_probabilities={
                "has_independent_verifiable_work": independent_work,
                "requires_iterative_delivery": iterative_delivery,
            },
            duration_ms=int((time.monotonic() - started) * 1000),
            state_chars=len(json.dumps(state, ensure_ascii=False)),
        ),
        usage_context,
    )
    return result


def _redact(value: Any, *, limit: int = 1000) -> Any:
    if isinstance(value, dict):
        return {
            str(key): "[redacted]"
            if any(part in str(key).lower() for part in _SENSITIVE_KEY_PARTS)
            else _redact(item, limit=limit)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item, limit=limit) for item in value]
    if isinstance(value, str):
        return value[:limit]
    return value


def _summary(value: Any, limit: int) -> str:
    return json.dumps(_redact(value, limit=limit), ensure_ascii=False, default=str)[:limit]


def evaluate_goal_with_typesafe(
    payload: dict[str, Any],
    *,
    usage_context: dict[str, Any] | None = None,
    client_factory: Callable[[float], tuple[Any, Any, Any]] | None = None,
):
    """Map TypeSafe completion evidence judgments onto the existing Evaluation type."""
    started = time.monotonic()
    state = {
        "goal_condition": _bounded(payload.get("condition"), 3000),
        "final_claim": _bounded(payload.get("final_claim"), 3000),
        "evidence": [
            _summary(item, 350) for item in list(payload.get("evidence") or [])[-20:]
        ],
        "recent_messages": [
            _summary(item, 300)
            for item in list(payload.get("recent_messages") or [])[-8:]
        ],
        "deterministic_gate": {
            "bindings_closed": True,
            "hard_acceptance": "not_applicable_or_passed",
            "unresolved_mutations": [],
        },
    }

    def questions(Choice: Any, Noul: Any) -> dict[str, Any]:
        return {
            "completion_state": Choice(
                instructions="Does the evidence prove that the Goal condition is complete?",
                criteria={
                    "completed": "The Goal condition is fulfilled and concrete evidence supports the final claim.",
                    "incomplete": "The Goal condition is not yet fulfilled.",
                    "insufficient_evidence": "The available evidence cannot prove the Goal condition is fulfilled.",
                },
            ),
            "claim_supported_by_evidence": Noul(
                instructions="Do the tool evidence and final claim directly support the Goal condition?"
            ),
            "deliverable_or_verification_present": Noul(
                instructions="Is there observable evidence of the required deliverable or verification?"
            ),
        }

    response = _system_one(
        state=state,
        build_questions=questions,
        timeout_s=_setting("goal_timeout_s", 20.0),
        client_factory=client_factory,
    )
    completion, confidence = _choice(
        response,
        "completion_state",
        frozenset({"completed", "incomplete", "insufficient_evidence"}),
    )
    claim_supported = _noul(response, "claim_supported_by_evidence")
    deliverable_present = _noul(response, "deliverable_or_verification_present")
    confidence_threshold = _setting("goal_completion_min_confidence", 0.70)
    evidence_threshold = 0.70
    completed = (
        completion == "completed"
        and confidence >= confidence_threshold
        and claim_supported >= evidence_threshold
        and deliverable_present >= evidence_threshold
    )
    missing: list[str] = []
    if completion == "insufficient_evidence":
        missing.append("goal_completion_evidence")
    elif completion != "completed" or confidence < confidence_threshold:
        missing.append("goal_completion_evidence")
    if claim_supported < evidence_threshold:
        missing.append("final_claim_support")
    if deliverable_present < evidence_threshold:
        missing.append("deliverable_or_verification")
    if completed:
        reason = "TypeSafe：Goal 完成判断与证据门槛均已满足"
        next_action = ""
    elif completion == "insufficient_evidence":
        reason = "TypeSafe：现有证据不足以证明 Goal 已完成"
        next_action = "继续执行并收集与 Goal 条件直接对应的可验证证据"
    elif completion == "incomplete":
        reason = "TypeSafe：Goal 尚未完成"
        next_action = "继续执行并收集与 Goal 条件直接对应的可验证证据"
    else:
        reason = "TypeSafe：完成判断或证据门槛不足"
        next_action = "继续执行并收集与 Goal 条件直接对应的可验证证据"

    from server.runtime.goal import Evaluation

    result = Evaluation(
        completed=completed,
        reason=reason,
        missing_evidence=missing,
        next_action=next_action,
        model_profile_id="typesafe:jev",
    )
    _append_audit(
        TypeSafeDecisionAudit(
            operation="goal_evaluation",
            route="typesafe",
            choice=completion,
            confidence=confidence,
            noul_probabilities={
                "claim_supported_by_evidence": claim_supported,
                "deliverable_or_verification_present": deliverable_present,
            },
            duration_ms=int((time.monotonic() - started) * 1000),
            state_chars=len(json.dumps(state, ensure_ascii=False, default=str)),
        ),
        usage_context,
    )
    return result
