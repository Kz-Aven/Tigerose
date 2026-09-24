"""Compatibility bridge from the legacy Goal controller to workflow events.

V0 records observability-only shadow data.  V1.1 additionally lets the Stop
Gate read that projection, but never lets a failed shadow write change a legacy
Goal decision.
"""

from __future__ import annotations

import logging
from typing import Any

from server.runtime.feature_flags import flag_enabled

_LOG = logging.getLogger(__name__)


def shadow_enabled() -> bool:
    return flag_enabled("durable_workflow_shadow_v0")


def projection_enabled() -> bool:
    return (
        shadow_enabled()
        and flag_enabled("durable_workflow_v1")
        and flag_enabled("goal_workflow_projection_v1")
    )


def activate(goal: dict[str, Any], *, owner_session_id: str) -> bool:
    """Create or recover the one shadow workflow for a legacy Goal."""
    if not shadow_enabled() or not goal.get("goal_id"):
        return False
    try:
        from server.db import workflow_repos

        workflow = workflow_repos.create_workflow(
            kind="legacy_goal",
            owner_session_id=owner_session_id,
            title=str(goal.get("condition") or "Goal"),
            intent={
                "condition": str(goal.get("condition") or ""),
                "source": str(goal.get("source") or ""),
                "goal_id": str(goal["goal_id"]),
            },
            executor_kind="goal_projection",
            acceptance_policy="legacy_goal_stop_gate",
            legacy_source="goal",
            legacy_id=str(goal["goal_id"]),
        )
        root = workflow_repos.get_root_task(str(workflow["workflow_id"]))
        if not root:
            return False
        goal["workflow_id"] = workflow["workflow_id"]
        goal["workflow_root_task_id"] = root["task_id"]
        return True
    except Exception:
        _LOG.exception("durable workflow Goal activation shadow failed")
        return False


def record_evidence(goal: dict[str, Any], evidence: dict[str, Any]) -> None:
    if not shadow_enabled():
        return
    workflow_id = str(goal.get("workflow_id") or "")
    task_id = str(goal.get("workflow_root_task_id") or "")
    if not workflow_id:
        return
    try:
        from server.db import workflow_repos

        workflow_repos.append_event(
            workflow_id,
            "GoalEvidence",
            task_id=task_id or None,
            payload={"evidence": evidence},
            dedupe_key=f"goal-evidence:{evidence.get('evidence_id') or ''}",
        )
    except Exception:
        _LOG.exception("durable workflow Goal evidence shadow failed")


def project_bindings(goal: dict[str, Any], bindings: list[dict[str, Any]]) -> None:
    if not projection_enabled():
        return
    workflow_id = str(goal.get("workflow_id") or "")
    task_id = str(goal.get("workflow_root_task_id") or "")
    if not workflow_id or not task_id:
        return
    try:
        from server.db import workflow_repos

        workflow_repos.project_goal_bindings(workflow_id, task_id, bindings=bindings)
    except Exception:
        _LOG.exception("durable workflow Goal bindings projection failed")


def terminal(goal: dict[str, Any], *, reason: str = "") -> None:
    if not projection_enabled():
        return
    workflow_id = str(goal.get("workflow_id") or "")
    task_id = str(goal.get("workflow_root_task_id") or "")
    if not workflow_id or not task_id:
        return
    try:
        from server.db import workflow_repos

        workflow_repos.project_goal_terminal(
            workflow_id,
            task_id,
            goal_status=str(goal.get("status") or ""),
            reason=reason,
        )
    except Exception:
        _LOG.exception("durable workflow Goal terminal projection failed")


def resume(goal: dict[str, Any]) -> None:
    if not projection_enabled():
        return
    workflow_id = str(goal.get("workflow_id") or "")
    task_id = str(goal.get("workflow_root_task_id") or "")
    if not workflow_id or not task_id:
        return
    try:
        from server.db import workflow_repos

        workflow_repos.reopen_goal_projection(workflow_id, task_id)
    except Exception:
        _LOG.exception("durable workflow Goal resume projection failed")


def projected_bindings(goal: dict[str, Any]) -> list[dict[str, Any]] | None:
    if not projection_enabled():
        return None
    workflow_id = str(goal.get("workflow_id") or "")
    if not workflow_id:
        return None
    try:
        from server.db import workflow_repos

        return workflow_repos.latest_goal_bindings(workflow_id)
    except Exception:
        _LOG.exception("durable workflow Goal binding read failed")
        return None


def projected_evidence(goal: dict[str, Any]) -> list[dict[str, Any]] | None:
    if not projection_enabled():
        return None
    workflow_id = str(goal.get("workflow_id") or "")
    if not workflow_id:
        return None
    try:
        from server.db import workflow_repos

        return workflow_repos.goal_evidence(workflow_id)
    except Exception:
        _LOG.exception("durable workflow Goal evidence read failed")
        return None
