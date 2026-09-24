"""Semantic task-intent classification for execution labels and Goal activation."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

TaskIntent = Literal["conversation", "read_only", "one_shot_action", "durable_goal"]
GoalRelationship = Literal[
    "goal_control",
    "goal_continuation",
    "independent",
    "goal_switch",
    "none",
]

_GOAL_ACTIVATION_MIN_CONFIDENCE = 0.7


@dataclass
class TaskIntentResult:
    intent: TaskIntent
    confidence: float
    durable_signals: list[str] = field(default_factory=list)
    reason: str = ""
    goal_relationship: GoalRelationship = "none"
    objective_id: str | None = None
    authoritative: bool = False
    classified: bool = True
    classification_error: str = ""


def heuristic_intent(text: str) -> TaskIntentResult | None:
    """Deprecated compatibility entry point.

    Intent labels are semantic model output only. Keeping this no-op avoids callers
    silently recovering the old keyword-based routing behavior.
    """
    _ = text
    return None


def is_obviously_non_goal(text: str) -> bool:
    """Deprecated compatibility entry point; semantic classification is required."""
    _ = text
    return False


def should_activate_goal(result: TaskIntentResult) -> bool:
    from avent_config import cfg_get, get_config

    threshold = cfg_get(
        get_config(),
        "agent",
        "typesafe",
        "intent_goal_activation_min_confidence",
        default=_GOAL_ACTIVATION_MIN_CONFIDENCE,
    )
    try:
        threshold = float(threshold)
    except (TypeError, ValueError):
        threshold = _GOAL_ACTIVATION_MIN_CONFIDENCE
    return (
        result.intent == "durable_goal"
        and result.confidence >= threshold
        and bool(result.durable_signals)
        and result.authoritative
    )


def _parse_classifier_json(raw: str) -> TaskIntentResult | None:
    start = raw.find("{")
    if start < 0:
        return None
    try:
        data, _ = json.JSONDecoder().raw_decode(raw[start:])
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None

    intent_raw = str(data.get("intent") or "").strip()
    valid_intents = {"conversation", "read_only", "one_shot_action", "durable_goal"}
    if intent_raw not in valid_intents:
        return None

    confidence_raw = data.get("confidence", 0.0)
    try:
        confidence = float(confidence_raw)
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    durable_signals = data.get("durable_signals")
    if not isinstance(durable_signals, list):
        durable_signals = []
    durable_signals = [str(item) for item in durable_signals if str(item).strip()]

    relationship_raw = str(data.get("goal_relationship") or "none").strip()
    valid_relationships = {
        "goal_control",
        "goal_continuation",
        "independent",
        "goal_switch",
        "none",
    }
    goal_relationship: GoalRelationship = (
        relationship_raw if relationship_raw in valid_relationships else "none"
    )

    objective_id = data.get("objective_id")
    objective_id = str(objective_id).strip() if objective_id else None

    return TaskIntentResult(
        intent=intent_raw,  # type: ignore[arg-type]
        confidence=confidence,
        durable_signals=durable_signals,
        reason=str(data.get("reason") or "").strip(),
        goal_relationship=goal_relationship,
        objective_id=objective_id,
        authoritative=True,
    )


def _unavailable_intent(reason: str) -> TaskIntentResult:
    """Do not invent a task label when semantic classification is unavailable."""
    return TaskIntentResult(
        intent="read_only",
        confidence=0.0,
        reason="任务类型识别中",
        authoritative=False,
        classified=False,
        classification_error=reason,
    )


def classify_task_intent_with_llm(
    text: str,
    profile: dict[str, Any],
    client_factory: Callable[[dict[str, Any]], Any],
    usage_context: dict[str, Any] | None = None,
) -> TaskIntentResult:
    prompt = (
        "Classify the user's request by its full semantic task shape. "
        "Do not use keywords, action verbs, or entity names as a shortcut. "
        "Return ONLY JSON with keys: "
        "intent (conversation|read_only|one_shot_action|durable_goal), "
        "confidence (0-1), durable_signals (array of semantic task facts), "
        "reason (string), goal_relationship "
        "(goal_control|goal_continuation|independent|goal_switch|none). "
        "A durable goal has multiple independently verifiable work items, "
        "dependencies, or an iterative delivery loop. A one-shot action is one "
        "bounded action with one expected result."
    )
    try:
        client = client_factory(profile)
        model = str(profile.get("id") or "").strip()
        from server.runtime.usage import create_completion

        resp = create_completion(
            client.with_options(timeout=5.0),
            profile=profile,
            call_kind="intent_classifier",
            **(usage_context or {}),
            model=model,
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": text[:4000]},
            ],
            max_tokens=200,
        )
        from server.runtime.goal import extract_assistant_text

        raw = extract_assistant_text(resp.choices[0].message)
        parsed = _parse_classifier_json(raw)
        if parsed is None:
            return _unavailable_intent("classifier returned invalid JSON")
        if parsed.intent == "durable_goal" and not parsed.durable_signals:
            return _unavailable_intent("durable_goal missing semantic task facts")
        return parsed
    except Exception as exc:
        return _unavailable_intent(f"classifier unavailable: {type(exc).__name__}")


def classify_task_intent(
    text: str,
    profile: dict[str, Any],
    client_factory: Callable[[dict[str, Any]], Any],
    usage_context: dict[str, Any] | None = None,
    *,
    active_goal_condition: str = "",
) -> TaskIntentResult:
    """Use TypeSafe first; only its technical failures use the legacy LLM."""
    try:
        from server.runtime.feature_flags import flag_enabled

        typesafe_enabled = flag_enabled("typesafe_judgments_v1")
    except Exception:
        typesafe_enabled = True
    if not typesafe_enabled:
        return classify_task_intent_with_llm(text, profile, client_factory, usage_context)

    try:
        from server.runtime.typesafe_judgments import (
            TypeSafeUnavailable,
            classify_task_intent_with_typesafe,
        )

        return classify_task_intent_with_typesafe(
            text,
            active_goal_condition=active_goal_condition,
            usage_context=usage_context,
        )
    except TypeSafeUnavailable as exc:
        from server.runtime.typesafe_judgments import record_typesafe_unavailable

        record_typesafe_unavailable("task_intent", exc, usage_context)
        return classify_task_intent_with_llm(text, profile, client_factory, usage_context)
