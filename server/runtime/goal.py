"""Persistent Goal state, slash commands, evidence binding, and Stop Gate."""

from __future__ import annotations

import json
import hashlib
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from server.runtime.tools import board

GOAL_ACTIVE = "active"
TERMINAL_GOAL_STATUSES = {
    "achieved",
    "cleared",
    "exhausted",
    "suspended",
    "cancelled",
}
MAX_BLOCKED_ROUNDS = 8
MAX_EVALUATION_BLOCKS = 8
_CLEAR_ALIASES = {"clear", "stop", "off", "reset", "none", "cancel"}
_RESUME_ALIASES = {"resume", "继续", "恢复", "继续执行", "继续任务"}
_AUTO_TOOL_NAMES = {
    "todo_write",
    "create_task",
    "claim_task",
    "write_file",
    "edit_file",
    "bash",
    "excel_write",
    "task",
}
# Tools that never count as meaningful Goal progress (fake-progress fence).
_TRIVIAL_PROGRESS_TOOLS = frozenset(
    {
        "read_file",
        "glob",
        "web_search",
        "web_extract",
        "list_tasks",
        "get_task",
        "get_skill",
        "check_inbox",
        "excel_read",
        "list_crons",
        "ask_user_question",
        "request_clear_session",
    }
)
# Readonly tools whose successful results may be cached by RunLoopGuard.
READONLY_CACHE_TOOLS = frozenset(
    {"read_file", "glob", "list_tasks", "get_task", "get_skill"}
)
# Temporary / gate-theatre tasks must not bind to the active Goal.
_GATE_THEATRE_TITLE = re.compile(
    r"(验证门控|clr[-_]?gate|clear.?gate|临时验证|gate.?check|stop.?gate|"
    r"清门控|unblock.?gate|test.?gate)",
    re.IGNORECASE,
)


def should_bind_task_to_goal(subject: str, *, bind_to_goal: bool | None = None) -> bool:
    """Return False for explicit opt-out or gate-theatre titles."""
    if bind_to_goal is False:
        return False
    if bind_to_goal is True:
        return True
    return not bool(_GATE_THEATRE_TITLE.search(subject or ""))


@dataclass(frozen=True)
class GoalCommand:
    action: str
    condition: str = ""


@dataclass(frozen=True)
class Evaluation:
    completed: bool
    reason: str
    missing_evidence: list[str]
    next_action: str
    model_profile_id: str = ""


def parse_goal_command(text: str) -> GoalCommand | None:
    match = re.search(r"(?:^|\s)/goal(?:\s+([^\n]+))?\s*$", text or "", re.IGNORECASE)
    if not match:
        return None
    arg = (match.group(1) or "").strip()
    if not arg:
        return GoalCommand("status")
    lowered = arg.lower()
    if lowered in _CLEAR_ALIASES:
        return GoalCommand("clear")
    if lowered in _RESUME_ALIASES:
        return GoalCommand("resume")
    return GoalCommand("set", arg)


def parse_evaluation_json(raw: str, *, profile_id: str = "") -> Evaluation:
    text = (raw or "").strip()
    if not text:
        raise ValueError("evaluator returned no JSON object")
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, re.IGNORECASE)
    if fence:
        text = fence.group(1).strip() or text
    start = text.find("{")
    if start < 0:
        raise ValueError("evaluator returned no JSON object")
    decoder = json.JSONDecoder()
    try:
        data, _ = decoder.raw_decode(text[start:])
    except json.JSONDecodeError as exc:
        raise ValueError(f"evaluator returned invalid JSON: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("completed"), bool):
        raise ValueError("invalid evaluator schema")
    missing = data.get("missing_evidence") or []
    if not isinstance(missing, list):
        raise ValueError("missing_evidence must be a list")
    return Evaluation(
        completed=data["completed"],
        reason=str(data.get("reason") or "")[:1000],
        missing_evidence=[str(v)[:300] for v in missing[:20]],
        next_action=str(data.get("next_action") or "")[:1000],
        model_profile_id=profile_id,
    )


def extract_assistant_text(message: Any) -> str:
    """Normalize assistant text, including DeepSeek reasoning_content fallbacks."""
    content = getattr(message, "content", None)
    if content is not None and str(content).strip():
        return str(content).strip()
    for attr in ("reasoning_content", "reasoning"):
        alt = getattr(message, attr, None)
        if alt and str(alt).strip():
            return str(alt).strip()
    extra = getattr(message, "model_extra", None)
    if isinstance(extra, dict):
        for key in ("reasoning_content", "reasoning"):
            alt = extra.get(key)
            if alt and str(alt).strip():
                return str(alt).strip()
    dump = getattr(message, "model_dump", None)
    if callable(dump):
        try:
            data = dump()
        except Exception:
            data = None
        if isinstance(data, dict):
            for key in ("content", "reasoning_content", "reasoning"):
                alt = data.get(key)
                if alt and str(alt).strip():
                    return str(alt).strip()
    return ""


def is_obviously_substantial(text: str) -> bool:
    """Deprecated compatibility entry point; keyword routing is intentionally gone."""
    _ = text
    return False


class GoalController:
    def __init__(
        self,
        *,
        state: Any,
        session_manager: Any,
        board_scope: str,
        run_id: str,
        user_request: str,
        evaluator: Callable[[dict[str, Any]], Evaluation] | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        cancel_check: Callable[[], None] | None = None,
        run_token: Any = None,
    ) -> None:
        self.state = state
        self.session_manager = session_manager
        self.board_scope = board_scope
        self.run_id = run_id
        self.user_request = user_request
        self.evaluator = evaluator
        self.on_event = on_event
        self.cancel_check = cancel_check
        self.run_token = run_token
        self._call_fingerprints: dict[str, str] = {}
        # When False, MCP/write tools do not auto-create a durable Goal (one-shot path).
        self.auto_activate_tools = True
        goal = self.goal
        if goal and goal.get("goal_id"):
            try:
                from server.db import repos

                restored = repos.list_goal_task_ids(str(goal["goal_id"]))
                bound = goal.setdefault("task_ids", [])
                for task_id in restored:
                    if task_id not in bound:
                        bound.append(task_id)
            except Exception:
                pass

    @property
    def goal(self) -> dict[str, Any] | None:
        value = self.state.context.get("goal")
        return value if isinstance(value, dict) else None

    @property
    def active(self) -> bool:
        return bool(self.goal and self.goal.get("status") == GOAL_ACTIVE)

    def _emit(self, name: str, detail: str = "") -> None:
        event = {
            "name": name,
            "detail": detail[:500],
            "run_id": self.run_id,
            "ts": time.time(),
        }
        goal = self.goal
        if goal is not None:
            goal.setdefault("lifecycle_events", []).append(event)
            goal["lifecycle_events"] = goal["lifecycle_events"][-100:]
            self._save()
        try:
            from server.runtime.run_transcript import append_run_event

            append_run_event(
                self.run_id,
                {"kind": "goal_event", "name": name, "detail": detail[:500]},
            )
        except Exception:
            pass
        if self.on_event:
            self.on_event(
                {
                    "kind": "hook",
                    **event,
                }
            )

    def _save(self, *, force: bool = False) -> None:
        if self.run_token is not None and not force:
            try:
                from server.runtime.run_coordinator import coordinator

                if not coordinator.can_commit(self.run_token):
                    return
            except Exception:
                pass
        self.session_manager.save(
            self.state,
            expected_version=self.state.version,
            retries=5,
        )

    @staticmethod
    def _is_meaningful_progress(
        name: str,
        *,
        outcome: str,
        transition: Any,
        metadata: dict[str, Any],
    ) -> bool:
        if outcome != "ok":
            return False
        if metadata.get("acceptance_ok") is True:
            return True
        if (
            isinstance(transition, dict)
            and transition.get("ok")
            and transition.get("old_status") != transition.get("new_status")
        ):
            return True
        if name in _TRIVIAL_PROGRESS_TOOLS:
            return False
        if name in {"write_file", "edit_file", "bash", "excel_write"}:
            from server.runtime.acceptance import path_is_under_user_plugins

            for key in ("path", "target", "dest"):
                raw = metadata.get(key) or ""
                if raw and path_is_under_user_plugins(str(raw)):
                    return True
            summary = str(metadata.get("summary") or "")
            if "Application Support/Tigerose/plugins" in summary or "/plugins/" in summary:
                # Only count if acceptance layer tagged it; otherwise treat as non-meaningful.
                return bool(metadata.get("acceptance_ok"))
            return False
        if name in {"complete_task", "claim_task", "fail_task", "cancel_task", "todo_write"}:
            return True
        # External MCP mutations (calendar create, contacts write, etc.) count.
        if name.startswith("mcp__"):
            kind = str(metadata.get("tool_kind") or "")
            if kind in ("", "mutation"):
                return True
        return False

    def activate(self, condition: str, *, source: str) -> dict[str, Any]:
        now = time.time()
        current_todo_ids = {
            str(t.get("id")) for t in self.state.todos if isinstance(t, dict) and t.get("id")
        }
        current_task_ids = {
            str(t.get("task_id")) for t in board.list_tasks(self.board_scope)
        }
        goal = {
            "goal_id": f"goal_{uuid.uuid4().hex[:12]}",
            "condition": condition.strip(),
            "source": source,
            "status": GOAL_ACTIVE,
            "created_at": now,
            "updated_at": now,
            "blocked_rounds": 0,
            "evaluation_blocks": 0,
            "total_blocks": 0,
            "progress_epoch": 0,
            "last_block_progress_epoch": 0,
            "seen_progress_fingerprints": [],
            "last_progress": {},
            "run_id": self.run_id,
            "cancel_requested": False,
            "todo_ids": [],
            "task_ids": [],
            "todo_baseline": sorted(current_todo_ids),
            "task_baseline": sorted(current_task_ids),
            "evidence": [],
            "lifecycle_events": [],
            "last_evaluation": {},
        }
        self.state.context["goal"] = goal
        self._save()
        self._emit("GoalActivated", f"{source}: {condition}")
        try:
            from server.runtime import workflow_shadow

            if workflow_shadow.activate(
                goal, owner_session_id=str(self.state.meta.session_id)
            ):
                self._save()
        except Exception:
            # Shadow persistence is deliberately never allowed to alter Goal flow.
            pass
        return goal

    def bind_objective(self, objective_id: str) -> None:
        """Keep Goal failure evaluation isolated to the objective of this run."""
        goal = self.goal
        if goal and objective_id:
            goal["objective_id"] = objective_id
            self._save()

    def clear(self) -> dict[str, Any] | None:
        goal = self.goal
        if goal:
            goal["status"] = "cleared"
            goal["updated_at"] = time.time()
            self._save()
            try:
                from server.runtime import workflow_shadow

                workflow_shadow.terminal(goal, reason="cleared")
            except Exception:
                pass
        return goal

    def resume(self) -> dict[str, Any] | None:
        goal = self.goal
        if goal and goal.get("status") == "suspended":
            goal["status"] = GOAL_ACTIVE
            goal["run_id"] = self.run_id
            goal["cancel_requested"] = False
            goal["blocked_rounds"] = 0
            goal["evaluation_blocks"] = 0
            goal["updated_at"] = time.time()
            self._save(force=True)
            # Drop deleted/stale bindings before the next stop-gate cycle.
            self.pending_items()
            self._emit("GoalActivated", "resumed")
            try:
                from server.runtime import workflow_shadow

                workflow_shadow.resume(goal)
            except Exception:
                pass
            return goal
        return None

    def mark_waiting_for_user(self, question_id: str) -> None:
        goal = self.goal
        if not goal:
            return
        goal["status"] = "waiting_for_user"
        goal["pending_question_id"] = question_id
        goal["updated_at"] = time.time()
        self._save()
        self._emit("GoalWaitingForUser", question_id)

    def resume_waiting_for_user(self, question_id: str) -> dict[str, Any] | None:
        goal = self.goal
        if (
            goal
            and goal.get("status") == "waiting_for_user"
            and goal.get("pending_question_id") == question_id
        ):
            goal["status"] = GOAL_ACTIVE
            goal["run_id"] = self.run_id
            goal.pop("pending_question_id", None)
            goal["updated_at"] = time.time()
            self._save()
            self._emit("GoalActivated", f"answered {question_id}")
            return goal
        return None

    def status_text(self) -> str:
        goal = self.goal
        if not goal:
            return "当前没有 Goal。"
        evaluation = goal.get("last_evaluation") or {}
        reason = str(evaluation.get("reason") or "尚未评估")
        recent_events = ", ".join(
            str(event.get("name") or "")
            for event in (goal.get("lifecycle_events") or [])[-3:]
            if isinstance(event, dict)
        )
        snap = self.acceptance_snapshot()
        return (
            f"Goal: [{goal.get('status')}] {goal.get('condition')}\n"
            f"bindings_closed: {snap['bindings_closed']} | "
            f"acceptance_ok: {snap['acceptance_ok']} | "
            f"gate_reason: {snap['gate_reason']}\n"
            f"GoalCriteria blocks: {int(goal.get('evaluation_blocks') or 0)}/{MAX_EVALUATION_BLOCKS}\n"
            f"RuntimeWarnings: {', '.join(snap.get('runtime_warnings') or []) or '尚无'}\n"
            f"连续无进展: {int(goal.get('blocked_rounds') or 0)}/{MAX_BLOCKED_ROUNDS}\n"
            f"最近进展: {(goal.get('last_progress') or {}).get('summary') or '尚无'}\n"
            f"最近评估: {reason}\n"
            f"生命周期: {recent_events or '尚无'}"
        )

    def before_tool(self, name: str, args: dict[str, Any], tool_call_id: str) -> None:
        raw = json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
        self._call_fingerprints[tool_call_id] = hashlib.sha256(
            f"{name}\0{raw}".encode("utf-8")
        ).hexdigest()[:20]
        if (
            self.auto_activate_tools
            and not self.active
            and (
                name in _AUTO_TOOL_NAMES
                or name.startswith("mcp__")
                or name.startswith("write_")
                or name.startswith("edit_")
            )
        ):
            self.activate(self.user_request, source="auto_tool")
        goal = self.goal
        if goal and self.active:
            goal["run_id"] = self.run_id
            goal["updated_at"] = time.time()

    def after_tool(self, result: Any) -> None:
        if not self.active:
            return
        goal = self.goal
        if not goal:
            return
        metadata = dict(getattr(result, "metadata", {}) or {})
        name = str(metadata.get("tool_name") or "")
        outcome = str(getattr(result, "outcome", "error"))
        todo_ids = [str(v) for v in metadata.get("todo_ids") or []]
        baseline = set(goal.get("todo_baseline") or [])
        for todo_id in todo_ids:
            if todo_id not in baseline and todo_id not in goal["todo_ids"]:
                goal["todo_ids"].append(todo_id)
        transition = metadata.get("task_transition")
        if isinstance(transition, dict) and transition.get("ok"):
            task_id = str(transition.get("task_id") or "")
            scope = str(transition.get("scope_key") or "")
            new_status = str(transition.get("new_status") or "")
            bound = goal.setdefault("task_ids", [])
            if task_id and (
                name == "delete_task"
                or new_status in {"missing", "deleted", "archived"}
            ):
                # Deleted/archived tasks must leave the Goal binding set.
                goal["task_ids"] = [t for t in bound if str(t) != task_id]
            elif (
                task_id
                and scope == self.board_scope
                and task_id not in bound
                and metadata.get("bind_to_goal", True) is not False
            ):
                title = str(
                    metadata.get("task_title")
                    or transition.get("title")
                    or ""
                )
                if not title:
                    task = board.get_task(task_id)
                    title = str((task or {}).get("title") or "")
                if should_bind_task_to_goal(
                    title,
                    bind_to_goal=metadata.get("bind_to_goal"),
                ):
                    bound.append(task_id)
        evidence = {
            "kind": "tool_result",
            "evidence_id": f"ev_{uuid.uuid4().hex[:12]}",
            "tool_call_id": str(metadata.get("tool_call_id") or ""),
            "name": name,
            "action_fingerprint": self._call_fingerprints.get(
                str(metadata.get("tool_call_id") or ""), ""
            ),
            "summary": str(getattr(result, "content", ""))[:500],
            "success": outcome == "ok",
            "outcome": outcome,
            "exit_code": metadata.get("exit_code"),
            "acceptance_ok": metadata.get("acceptance_ok"),
            "recoverable_deny": bool(metadata.get("recoverable_deny")),
            "loop_guard": bool(metadata.get("loop_guard")),
            "permission_denied": bool(metadata.get("permission_denied")),
            "tool_kind": str(metadata.get("tool_kind") or ""),
            "goal_role": str(metadata.get("goal_role") or ""),
            "tool_effect": str(metadata.get("tool_effect") or ""),
            "scope_violation": bool(metadata.get("scope_violation")),
            "cached_readonly": bool(metadata.get("cached_readonly")),
            "todo_id": todo_ids[0] if len(todo_ids) == 1 else None,
            "task_id": (
                str(transition.get("task_id")) if isinstance(transition, dict) else None
            ),
            "ts": time.time(),
        }
        goal.setdefault("evidence", []).append(evidence)
        # Preview-only window for UI; authoritative unresolved uses full scan of
        # evidence list plus attempt ledger when present (see unresolved_tool_failures).
        goal["evidence_preview"] = list(goal.get("evidence") or [])[-50:]
        # Migration: keep evidence list but cap growth to avoid unbounded session JSON.
        # Failures remain in attempt ledger; do not use truncation to clear gates.
        if len(goal["evidence"]) > 200:
            goal["evidence"] = goal["evidence"][-200:]
        goal["evidence"] = goal.get("evidence") or []
        subagent_status = str(metadata.get("subagent_status") or "")
        if subagent_status in {"max_rounds", "error", "cancelled"}:
            failed_ids: list[str] = []
            for task_id in goal.get("task_ids") or []:
                task = board.get_task(str(task_id))
                if (
                    task
                    and task.get("scope_key") == self.board_scope
                    and task.get("status") == "in_progress"
                ):
                    board.fail_task(
                        str(task_id),
                        f"sub-agent {subagent_status}",
                        expected_scope=self.board_scope,
                    )
                    failed_ids.append(str(task_id))
            if failed_ids:
                metadata["failed_task_ids"] = failed_ids
                evidence["failed_task_ids"] = failed_ids

        if outcome == "ok":
            content_hash = hashlib.sha256(
                str(getattr(result, "content", "")).encode("utf-8")
            ).hexdigest()[:16]
            progress_key = (
                f"{evidence.get('action_fingerprint') or name}:{content_hash}"
            )
            seen = goal.setdefault("seen_progress_fingerprints", [])
            meaningful = self._is_meaningful_progress(
                name,
                outcome=outcome,
                transition=transition,
                metadata={**metadata, "summary": evidence.get("summary")},
            )
            transition_changed = bool(
                isinstance(transition, dict)
                and transition.get("ok")
                and transition.get("old_status") != transition.get("new_status")
            )
            if meaningful and (progress_key not in seen or transition_changed):
                seen.append(progress_key)
                goal["seen_progress_fingerprints"] = seen[-200:]
                goal["progress_epoch"] = int(goal.get("progress_epoch") or 0) + 1
                # Meaningful progress resets consecutive no-progress rounds only.
                # evaluation_blocks is a hard ceiling and is never reset here.
                goal["blocked_rounds"] = 0
                goal["last_progress"] = {
                    "tool": name,
                    "summary": str(getattr(result, "content", ""))[:200],
                    "ts": time.time(),
                }
            elif progress_key not in seen:
                # Record fingerprint so repeats are visible, but do not reset counters.
                seen.append(progress_key)
                goal["seen_progress_fingerprints"] = seen[-200:]
        goal["updated_at"] = time.time()
        self._save()
        try:
            from server.runtime import workflow_shadow

            workflow_shadow.record_evidence(goal, evidence)
        except Exception:
            pass
        self._emit("GoalEvidence", f"{name}: {outcome}")

    def pending_items(self) -> list[str]:
        """Return incomplete bound items; prune deleted/missing bindings."""
        goal = self.goal
        if not goal:
            return []
        todos = {
            str(t.get("id")): str(t.get("status") or "pending")
            for t in self.state.todos
            if isinstance(t, dict) and t.get("id")
        }
        pending: list[str] = []
        bindings: list[dict[str, Any]] = []
        pruned = False

        live_todo_ids: list[str] = []
        for todo_id in list(goal.get("todo_ids") or []):
            tid = str(todo_id)
            if tid not in todos:
                # Todo was replaced away from the list — unbind, do not block forever.
                pruned = True
                continue
            live_todo_ids.append(tid)
            status = todos[tid]
            bindings.append({"kind": "todo", "id": tid, "status": status})
            if status not in {"completed", "cancelled"}:
                pending.append(f"todo {tid} [{status}]")
        if live_todo_ids != list(goal.get("todo_ids") or []):
            goal["todo_ids"] = live_todo_ids
            pruned = True

        live_task_ids: list[str] = []
        for task_id in list(goal.get("task_ids") or []):
            tid = str(task_id)
            task = board.get_task(tid)
            if not task or task.get("scope_key") != self.board_scope:
                # Deleted or out-of-scope: unbind. Never treat as permanent pending.
                pruned = True
                continue
            live_task_ids.append(tid)
            status = str(task.get("status") or "pending")
            bindings.append({"kind": "task", "id": tid, "status": status})
            if status not in {"completed", "cancelled", "failed", "archived"}:
                pending.append(f"task {tid} [{status}]")
        if live_task_ids != list(goal.get("task_ids") or []):
            goal["task_ids"] = live_task_ids
            pruned = True

        if pruned:
            goal["updated_at"] = time.time()
            self._save()
            self._emit(
                "GoalBindingsPruned",
                f"pending={len(pending)} tasks={len(live_task_ids)} todos={len(live_todo_ids)}",
            )
        try:
            from server.runtime import workflow_shadow

            workflow_shadow.project_bindings(goal, bindings)
            projected = workflow_shadow.projected_bindings(goal)
            if projected is not None:
                pending = [
                    f"{item.get('kind')} {item.get('id')} [{item.get('status')}]"
                    for item in projected
                    if str(item.get("status") or "pending")
                    not in {"completed", "cancelled", "failed", "archived"}
                ]
        except Exception:
            pass
        return pending

    def unresolved_tool_failures(self) -> list[str]:
        """Mutation failures only — Query/control/loop_guard never block Goal."""
        from server.runtime.tools.registry import CONTROL_TOOLS, QUERY_TOOLS

        # Prefer attempt ledger when present (authoritative; not truncated).
        try:
            from server.runtime.feature_flags import flag_enabled
            from server.runtime.attempt_ledger import AttemptLedger

            if flag_enabled("attempt_ledger_v2") and isinstance(
                getattr(self.state, "context", None), dict
            ):
                ledger = AttemptLedger.from_context(self.state.context)
                objective_id = str((self.goal or {}).get("objective_id") or "")
                blocking = ledger.blocking_attempts(objective_id or None)
                if blocking:
                    return [
                        f"{a.tool_name} [{a.status}]"
                        for a in blocking
                    ]
        except Exception:
            pass

        goal = self.goal or {}
        evidence_source = list(goal.get("evidence") or [])
        try:
            from server.runtime import workflow_shadow

            projected = workflow_shadow.projected_evidence(goal)
            if projected is not None:
                evidence_source = projected
        except Exception:
            pass
        unresolved: dict[str, tuple[str, str]] = {}
        for evidence in evidence_source:
            name = str(evidence.get("name") or "")
            fingerprint = str(evidence.get("action_fingerprint") or name)
            outcome = str(evidence.get("outcome") or "")
            if not name:
                continue
            kind = str(evidence.get("tool_kind") or "")
            goal_role = str(evidence.get("goal_role") or "")
            if not kind:
                if name in QUERY_TOOLS or goal_role == "query":
                    kind = "query"
                elif name in CONTROL_TOOLS or goal_role == "control":
                    kind = "control"
                else:
                    kind = "mutation"
            # Layer 2: Query / control never enter the unresolved registry.
            if kind in {"query", "control"} or goal_role in {"query", "control"}:
                unresolved.pop(fingerprint, None)
                continue
            # Scope violations are not Goal self-heal targets.
            if evidence.get("scope_violation"):
                unresolved.pop(fingerprint, None)
                continue
            # unknown goal_role: do not drive auto source repair
            if goal_role == "unknown":
                unresolved.pop(fingerprint, None)
                continue
            # RunLoopGuard rate-limits are not Goal failures.
            if evidence.get("loop_guard"):
                unresolved.pop(fingerprint, None)
                continue
            # Permission asks and acceptance soft denies are recoverable.
            if (
                evidence.get("permission_denied")
                or evidence.get("recoverable_deny")
                or evidence.get("acceptance_ok") is False
            ):
                if outcome in {"denied", "error"}:
                    unresolved.pop(fingerprint, None)
                    continue
            if outcome in {"error", "timeout", "denied", "cancelled"}:
                unresolved[fingerprint] = (name, outcome)
            elif outcome == "ok":
                unresolved.pop(fingerprint, None)
        return [
            f"{name} [{outcome}]"
            for name, outcome in sorted(unresolved.values())
        ]

    def runtime_warnings(self) -> list[str]:
        """P2: non-blocking process noise (Query errors, loop_guard, cache)."""
        goal = self.goal or {}
        warnings: list[str] = []
        evidence_source = list(goal.get("evidence") or [])
        try:
            from server.runtime import workflow_shadow

            projected = workflow_shadow.projected_evidence(goal)
            if projected is not None:
                evidence_source = projected
        except Exception:
            pass
        for evidence in evidence_source[-20:]:
            name = str(evidence.get("name") or "")
            outcome = str(evidence.get("outcome") or "")
            kind = str(evidence.get("tool_kind") or "")
            if evidence.get("loop_guard"):
                warnings.append(f"{name}: loop_guard")
            elif kind == "query" and outcome in {"error", "timeout", "denied"}:
                warnings.append(f"{name}: {outcome} (query, non-blocking)")
            elif evidence.get("cached_readonly"):
                warnings.append(f"{name}: cached_readonly")
        # de-dupe preserve order
        seen: set[str] = set()
        out: list[str] = []
        for w in warnings:
            if w not in seen:
                seen.add(w)
                out.append(w)
        return out[-10:]

    def _projection_evidence(self) -> list[dict[str, Any]]:
        """Prefer V1.1's durable event stream while retaining the V0 fallback."""
        goal = self.goal or {}
        fallback = list(goal.get("evidence") or [])
        try:
            from server.runtime import workflow_shadow

            projected = workflow_shadow.projected_evidence(goal)
            if projected is not None:
                return projected
        except Exception:
            pass
        return fallback

    def acceptance_snapshot(self, final_claim: str = "") -> dict[str, Any]:
        """GoalAcceptance triple; hard acceptance ranks above unresolved failures."""
        from server.runtime.acceptance import (
            assert_plugin_install_for_texts,
            looks_like_plugin_install,
        )

        pending = self.pending_items()
        bindings_closed = not pending
        failures = self.unresolved_tool_failures()
        warnings = self.runtime_warnings()
        goal = self.goal or {}
        texts = (
            str(goal.get("condition") or ""),
            self.user_request,
            final_claim,
        )
        hard_applies = any(looks_like_plugin_install(t) for t in texts)
        denial = (
            assert_plugin_install_for_texts(*texts) if hard_applies else None
        )
        if hard_applies:
            acceptance_ok: bool | None = denial is None
        else:
            acceptance_ok = None
        # Order mirrors Stop Gate: bindings → hard accept → unresolved → evaluator
        if not bindings_closed:
            gate_reason = "bindings_open"
        elif hard_applies and denial:
            gate_reason = "acceptance_failed"
        elif hard_applies and denial is None:
            gate_reason = "acceptance_ok"
        elif failures:
            gate_reason = "unresolved_failures"
        else:
            gate_reason = "needs_evaluator"
        return {
            "bindings_closed": bindings_closed,
            "acceptance_ok": acceptance_ok,
            "gate_reason": gate_reason,
            "pending_items": pending,
            "unresolved_failures": failures,
            "runtime_warnings": warnings,
            "acceptance_denial": denial,
        }

    def context_prompt(self) -> str:
        if not self.active:
            return ""
        goal = self.goal or {}
        snap = self.acceptance_snapshot()
        warnings = snap.get("runtime_warnings") or []
        return (
            "## Active Goal (authoritative)\n"
            f"Condition: {goal.get('condition')}\n"
            f"bindings_closed: {snap['bindings_closed']}\n"
            f"acceptance_ok: {snap['acceptance_ok']}\n"
            f"gate_reason: {snap['gate_reason']}\n"
            f"Consecutive no-progress stops: {goal.get('blocked_rounds', 0)}/{MAX_BLOCKED_ROUNDS}\n"
            f"Evaluation blocks (GoalCriteria): {goal.get('evaluation_blocks', 0)}/{MAX_EVALUATION_BLOCKS}\n"
            f"Last progress: {(goal.get('last_progress') or {}).get('summary') or '(none)'}\n"
            f"Bound incomplete items: {snap['pending_items'] or '(none)'}\n"
            f"RuntimeWarnings (non-blocking): {warnings or '(none)'}\n"
            "Do not stop until GoalCriteria are met (bindings + hard acceptance / evaluator).\n"
            "Do not create temporary tasks only to clear Stop Gate. "
            "Do not retry Query tools to clear RuntimeWarnings."
        )

    def _evaluation_payload(self, final_claim: str) -> dict[str, Any]:
        goal = self.goal or {}
        return {
            "condition": str(goal.get("condition") or "")[:4000],
            "pending_items": self.pending_items(),
            "evidence": self._projection_evidence()[-20:],
            "recent_messages": list(self.state.messages)[-8:],
            "final_claim": final_claim[:4000],
        }

    def _successful_mutation_evidence(self) -> list[dict[str, Any]]:
        """Recent successful mutation / MCP tool results usable as completion proof."""
        goal = self.goal or {}
        out: list[dict[str, Any]] = []
        for evidence in self._projection_evidence():
            if not isinstance(evidence, dict):
                continue
            outcome = str(evidence.get("outcome") or "")
            if outcome != "ok":
                continue
            if evidence.get("loop_guard") or evidence.get("permission_denied"):
                continue
            summary = str(evidence.get("summary") or "")
            if summary.startswith("MCP error:"):
                continue
            if evidence.get("mcp_error") or evidence.get("metadata", {}).get("mcp_error"):
                continue
            name = str(evidence.get("name") or "")
            kind = str(evidence.get("tool_kind") or "")
            if name.startswith("mcp__") or kind == "mutation":
                out.append(evidence)
        return out

    def _has_domain_acceptance_evidence(self, final_claim: str = "") -> bool:
        """True when hard plugin acceptance or domain acceptance receipts exist."""
        snap = self.acceptance_snapshot(final_claim)
        if snap["acceptance_ok"] is True:
            return True
        for evidence in self._projection_evidence():
            if not isinstance(evidence, dict):
                continue
            if evidence.get("acceptance_ok") is True:
                return True
            if evidence.get("acceptance_receipt"):
                return True
        try:
            from server.runtime.feature_flags import flag_enabled
            from server.runtime.attempt_ledger import AttemptLedger, AttemptState
            from server.runtime.acceptance import ACCEPTORS, evaluate_acceptance
            from server.runtime.objectives import Objective

            if not flag_enabled("attempt_ledger_v2") or not isinstance(
                getattr(self.state, "context", None), dict
            ):
                return False
            ledger = AttemptLedger.from_context(self.state.context)
            for raw in ledger.attempts.values():
                attempt = AttemptState.from_dict(raw)
                if attempt.status not in {"succeeded", "reconciled_succeeded"}:
                    continue
                if attempt.acceptance_receipt:
                    return True
                acceptance_type = str(
                    (attempt.receipt or {}).get("acceptance_type")
                    or (attempt.metadata or {}).get("acceptance_type")
                    or ""
                )
                if not acceptance_type or acceptance_type not in ACCEPTORS:
                    continue
                obj_raw = ledger.objectives.get(attempt.objective_id)
                if not obj_raw:
                    continue
                decision = evaluate_acceptance(
                    acceptance_type,
                    intent="durable_goal",
                    objective=Objective.from_dict(obj_raw),
                    attempts=[attempt],
                    final_claim=final_claim,
                )
                if decision.status == "accepted":
                    return True
        except Exception:
            pass
        return False

    def _evidence_supports_completion(self, final_claim: str = "") -> bool:
        """True when Stop Gate prerequisites are met and real work succeeded."""
        if self.pending_items():
            return False
        if self.unresolved_tool_failures():
            return False
        if not self._successful_mutation_evidence():
            return False
        try:
            from server.runtime.feature_flags import flag_enabled

            if flag_enabled("domain_acceptance_v2"):
                return self._has_domain_acceptance_evidence(final_claim)
        except Exception:
            pass
        return True

    def _record_evaluation(self, evaluation: Evaluation) -> None:
        goal = self.goal
        if not goal:
            return
        goal["last_evaluation"] = {
            "completed": evaluation.completed,
            "reason": evaluation.reason,
            "missing_evidence": evaluation.missing_evidence,
            "next_action": evaluation.next_action,
            "model_profile_id": evaluation.model_profile_id,
            "ts": time.time(),
        }
        goal["updated_at"] = time.time()
        self._save()

    def _block(self, reason: str, missing: list[str], next_action: str) -> dict[str, Any]:
        goal = self.goal
        if not goal:
            return {"action": "allow"}
        progress_epoch = int(goal.get("progress_epoch") or 0)
        last_block_epoch = int(goal.get("last_block_progress_epoch") or 0)
        made_progress = progress_epoch > last_block_epoch
        if made_progress:
            goal["blocked_rounds"] = 0
        else:
            goal["blocked_rounds"] = int(goal.get("blocked_rounds") or 0) + 1
        # Hard ceiling: every Stop Gate block increments evaluation_blocks.
        goal["evaluation_blocks"] = int(goal.get("evaluation_blocks") or 0) + 1
        goal["last_block_progress_epoch"] = progress_epoch
        goal["total_blocks"] = int(goal.get("total_blocks") or 0) + 1
        goal["last_evaluation"] = {
            "completed": False,
            "reason": reason,
            "missing_evidence": missing,
            "next_action": next_action,
            "ts": time.time(),
        }
        suspended = (
            goal["blocked_rounds"] >= MAX_BLOCKED_ROUNDS
            or goal["evaluation_blocks"] >= MAX_EVALUATION_BLOCKS
        )
        if suspended:
            goal["status"] = "suspended"
        goal["updated_at"] = time.time()
        self._save()
        self._emit("GoalSuspended" if suspended else "GoalBlocked", reason)
        if suspended:
            try:
                from server.runtime import workflow_shadow

                workflow_shadow.terminal(goal, reason=reason)
            except Exception:
                pass
            return {
                "action": "suspended",
                "message": (
                    f"Goal 已暂停：达到阻止上限 "
                    f"(连续无进展 {goal['blocked_rounds']}/{MAX_BLOCKED_ROUNDS}, "
                    f"评估阻止 {goal['evaluation_blocks']}/{MAX_EVALUATION_BLOCKS})。"
                    f"\n原因：{reason}\n可修正问题后使用 /goal resume。"
                ),
            }
        return {
            "action": "block",
            "message": (
                f"Stop Gate 阻止结束：{reason}\n"
                f"连续无进展：{goal['blocked_rounds']}/{MAX_BLOCKED_ROUNDS}\n"
                f"评估阻止：{goal['evaluation_blocks']}/{MAX_EVALUATION_BLOCKS}\n"
                f"缺失证据：{', '.join(missing) or '(未列出)'}\n"
                f"下一步：{next_action or '继续执行并收集完成证据'}\n"
                "不要为了清门控而创建临时/测试任务；完成真实工作或取消已绑定项。"
            ),
        }

    def _achieve(self, reason: str, *, terminal_heal: bool = False) -> dict[str, Any]:
        goal = self.goal
        if goal:
            goal["status"] = "achieved"
            goal["updated_at"] = time.time()
            if terminal_heal:
                goal["terminal_healed_at"] = time.time()
            goal["last_evaluation"] = {
                "completed": True,
                "reason": reason,
                "missing_evidence": [],
                "next_action": "",
                "ts": time.time(),
            }
            self._save()
        self._emit("GoalAchieved", reason)
        if goal:
            try:
                from server.runtime import workflow_shadow

                workflow_shadow.terminal(goal, reason=reason)
            except Exception:
                pass
        return {"action": "allow"}

    def on_candidate_stop(self, final_claim: str) -> dict[str, Any]:
        # Order: waiting → bindings → hard acceptance → mutation failures → evaluator
        if self.goal and self.goal.get("status") == "waiting_for_user":
            return {"action": "allow", "waiting_for_user": True}
        if not self.active:
            return {"action": "allow"}

        snap = self.acceptance_snapshot(final_claim)
        if not snap["bindings_closed"]:
            return self._block(
                "当前 Goal 仍有未完成的 Todo/Task",
                list(snap["pending_items"]),
                "完成或明确取消当前 Goal 绑定的项目（不要新建临时任务清门控）",
            )
        # Terminal > process: hard acceptance skip unresolved (retain evidence).
        if snap["acceptance_ok"] is False:
            denial = snap["acceptance_denial"] or "硬验收未通过"
            return self._block(
                f"硬性验收条件未满足：{denial}",
                ["plugin_install_acceptance"],
                "将插件安装到系统插件目录并确保 plugin.yaml 可加载后结束",
            )
        if snap["acceptance_ok"] is True:
            return self._achieve(
                "硬验收已通过（bindings 已闭环；跳过过程态 unresolved）",
                terminal_heal=True,
            )
        if snap["unresolved_failures"]:
            return self._block(
                "当前 Goal 仍有与目标直接相关的未完成写操作",
                list(snap["unresolved_failures"]),
                "仅重试与当前目标相关的终端动作；禁止修改 Runtime、检查无关数据库或创建临时任务。"
                "查询失败不会阻塞结束。",
            )

        if self.evaluator is None:
            goal = self.goal
            if goal:
                goal["status"] = "suspended"
                goal["updated_at"] = time.time()
                self._save()
            self._emit("GoalEvaluatorWarning", "evaluator unavailable")
            try:
                from server.runtime import workflow_shadow

                workflow_shadow.terminal(goal, reason="evaluator unavailable")
            except Exception:
                pass
            return {
                "action": "suspended",
                "message": "Goal evaluator unavailable; Goal 已暂停，请 /goal resume 后重试。",
            }
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                if self.cancel_check:
                    self.cancel_check()
                self._emit("GoalEvaluate", f"attempt {attempt + 1}")
                evaluation = self.evaluator(self._evaluation_payload(final_claim))
                if self.cancel_check:
                    self.cancel_check()
                self._record_evaluation(evaluation)
                if evaluation.completed:
                    # Re-check objective acceptance even if LLM says done.
                    from server.runtime.acceptance import assert_plugin_install_for_texts

                    denial = assert_plugin_install_for_texts(
                        str((self.goal or {}).get("condition") or ""),
                        self.user_request,
                        final_claim,
                        evaluation.reason,
                    )
                    if denial:
                        return self._block(
                            f"硬性验收条件未满足：{denial}",
                            ["plugin_install_acceptance"],
                            "完成系统插件目录安装后再结束",
                        )
                    return self._achieve(evaluation.reason)
                return self._block(
                    evaluation.reason or "评估器判定未完成",
                    evaluation.missing_evidence,
                    evaluation.next_action,
                )
            except Exception as exc:
                last_error = exc
                if attempt == 0:
                    time.sleep(0.1)
        # Evaluator transport/parse failure must not erase successful work.
        if self._evidence_supports_completion(final_claim):
            tools = [
                str(ev.get("name") or "")
                for ev in self._successful_mutation_evidence()[-3:]
            ]
            detail = ", ".join(t for t in tools if t) or "mutation tools"
            return self._achieve(
                f"评估器响应异常，但已有成功变更证据（{detail}），按完成处理"
                f"（{last_error}）"
            )
        # Spec: durable Goal with no deterministic evidence → suspend (not endless block).
        goal = self.goal
        if goal:
            goal["status"] = "suspended"
            goal["updated_at"] = time.time()
            goal["last_evaluation"] = {
                "completed": False,
                "reason": f"评估器暂时失败：{last_error}",
                "missing_evidence": ["evaluator_response"],
                "next_action": "使用 /goal resume 后重试",
                "ts": time.time(),
            }
            self._save()
        self._emit("GoalEvaluatorWarning", str(last_error))
        try:
            from server.runtime import workflow_shadow

            workflow_shadow.terminal(goal or {}, reason=str(last_error))
        except Exception:
            pass
        return {
            "action": "suspended",
            "message": (
                f"Goal evaluator 暂时失败：{last_error}；Goal 已暂停，"
                "请 /goal resume 后重试。"
            ),
        }

    def suspend_for_limit(self) -> None:
        if self.active and self.goal:
            self.goal["status"] = "suspended"
            self.goal["updated_at"] = time.time()
            self._save(force=True)
            try:
                from server.runtime import workflow_shadow

                workflow_shadow.terminal(self.goal, reason="run limit")
            except Exception:
                pass

    def cancel(self) -> None:
        if self.active and self.goal:
            self.goal["status"] = "cancelled"
            self.goal["cancel_requested"] = True
            self.goal["updated_at"] = time.time()
            self._save(force=True)
            self._emit("GoalCancelled", self.run_id)
            try:
                from server.runtime import workflow_shadow

                workflow_shadow.terminal(self.goal, reason=self.run_id)
            except Exception:
                pass
