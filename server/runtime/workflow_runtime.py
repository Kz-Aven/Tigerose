"""Single-process worker for durable workflow tasks.

Ownership is still SQLite based, so a later process can recover after this
thread stops.  Provider integrations register small adapters instead of giving
the scheduler knowledge of any external API.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from server.runtime.feature_flags import flag_enabled

Outcome = Literal["succeeded", "retryable_failure", "failed", "unknown"]

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExecutionResult:
    outcome: Outcome
    receipt: dict[str, Any] = field(default_factory=dict)
    error_code: str = ""
    retry_after_s: float = 0.0


Executor = Callable[[dict[str, Any], dict[str, Any]], ExecutionResult]
Reconciler = Callable[[dict[str, Any], dict[str, Any]], Literal["succeeded", "absent", "unknown"]]


class DurableWorkflowScheduler:
    def __init__(self, *, owner_id: str = "workflow-worker", poll_s: float = 1.0) -> None:
        self.owner_id = owner_id
        self.poll_s = max(0.1, poll_s)
        self._executors: dict[str, Executor] = {}
        self._reconcilers: dict[str, Reconciler] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def register_executor(self, kind: str, executor: Executor, *, reconciler: Reconciler | None = None) -> None:
        if not kind:
            raise ValueError("executor kind is required")
        self._executors[kind] = executor
        if reconciler is not None:
            self._reconcilers[kind] = reconciler

    def start(self) -> None:
        if not flag_enabled("durable_workflow_v1") or self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="durable-workflow", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join(timeout=self.poll_s * 2 + 1)

    def _run(self) -> None:
        while not self._stop.wait(self.poll_s):
            try:
                self.tick()
            except Exception:
                _LOG.exception("durable workflow scheduler tick failed")

    def tick(self) -> None:
        """Run a bounded recovery/claim/dispatch cycle for deterministic tests."""
        if not flag_enabled("durable_workflow_v1"):
            return
        from server.db import workflow_repos

        workflow_repos.recover_stale_dispatches()
        workflow_repos.expire_leases()
        workflow_repos.reap_dependency_blocks()
        task = workflow_repos.claim_next_task(
            self.owner_id, executor_kinds=set(self._executors)
        )
        if task is not None:
            lease = task["lease"]
            workflow = workflow_repos.get_workflow(str(task["workflow_id"])) or {}
            reservation_id = f"workflow:{task['task_id']}:{lease['lease_token']}"
            root_budget_id = str(workflow.get("root_budget_id") or "")
            if root_budget_id and not workflow_repos.reserve_budget(
                root_budget_id, reservation_id, "tool_call"
            ):
                workflow_repos.mark_task_waiting_for_budget(
                    str(task["task_id"]),
                    lease_token=str(lease["lease_token"]),
                    fencing_token=int(lease["fencing_token"]),
                )
                self._dispatch_one()
                return
            from server.runtime.workflow_context import build_context_pack

            context_pack = build_context_pack(str(task["task_id"]))
            attempt = workflow_repos.prepare_attempt(
                str(task["task_id"]),
                lease_token=str(lease["lease_token"]),
                fencing_token=int(lease["fencing_token"]),
                dispatch={
                    **dict(task.get("intent") or {}),
                    "context_pack_id": context_pack["pack_id"],
                    "budget_reservation_id": reservation_id if root_budget_id else "",
                },
            )
            if attempt is None:
                if root_budget_id:
                    workflow_repos.settle_budget_reservation(reservation_id, commit=False)
                _LOG.warning("workflow task lost its lease before attempt preparation: %s", task["task_id"])
        self._dispatch_one()

    def _dispatch_one(self) -> None:
        from server.db import workflow_repos

        item = workflow_repos.next_outbox_item()
        if item is None:
            return
        attempt_id = str(item.get("attempt_id") or "")
        if item.get("kind") == "reconcile_request":
            self._reconcile(item)
            return
        if item.get("kind") != "provider_dispatch" or not attempt_id:
            workflow_repos.finish_outbox_item(str(item["outbox_id"]), status="delivered")
            return
        attempt = workflow_repos.get_attempt(attempt_id)
        if attempt is None:
            workflow_repos.finish_outbox_item(str(item["outbox_id"]), status="failed")
            return
        task = workflow_repos.get_task(str(attempt["task_id"]))
        lease = workflow_repos.get_lease(str(attempt["task_id"]))
        if task is None or lease is None or lease.get("owner_id") != self.owner_id:
            workflow_repos.finish_outbox_item(str(item["outbox_id"]), status="pending", retry_at=time.time() + 1)
            return
        executor = self._executors.get(str(task.get("executor_kind") or ""))
        if executor is None:
            workflow_repos.fail_attempt(
                attempt_id,
                lease_token=str(lease["lease_token"]),
                fencing_token=int(lease["fencing_token"]),
                error_code="unsupported_executor",
            )
            workflow_repos.finish_outbox_item(str(item["outbox_id"]), status="delivered")
            return
        try:
            result = executor(task, attempt)
        except Exception:
            _LOG.exception("durable workflow executor raised")
            result = ExecutionResult("unknown", error_code="executor_exception")
        if result.outcome == "succeeded":
            workflow_repos.complete_attempt(attempt_id, lease_token=str(lease["lease_token"]), fencing_token=int(lease["fencing_token"]), receipt=result.receipt)
            merge_target = str(result.receipt.get("merge_target") or "")
            if merge_target:
                workflow_repos.mark_task_waiting_for_merge(str(task["task_id"]), merge_target=merge_target, receipt=result.receipt)
        elif result.outcome == "retryable_failure":
            workflow_repos.schedule_retry(
                attempt_id,
                lease_token=str(lease["lease_token"]),
                fencing_token=int(lease["fencing_token"]),
                error_code=result.error_code,
            )
        elif result.outcome == "failed":
            workflow_repos.fail_attempt(attempt_id, lease_token=str(lease["lease_token"]), fencing_token=int(lease["fencing_token"]), error_code=result.error_code)
        else:
            workflow_repos.mark_attempt_in_doubt(attempt_id, lease_token=str(lease["lease_token"]), fencing_token=int(lease["fencing_token"]), receipt=result.receipt)
        reservation_id = str((attempt.get("dispatch") or {}).get("budget_reservation_id") or "")
        if reservation_id:
            # An unknown provider result still consumed a dispatch budget; it must
            # never become free merely because recovery needs human confirmation.
            workflow_repos.settle_budget_reservation(reservation_id, commit=True)
        workflow_repos.finish_outbox_item(str(item["outbox_id"]), status="delivered")

    def _reconcile(self, item: dict[str, Any]) -> None:
        from server.db import workflow_repos

        attempt = workflow_repos.get_attempt(str(item.get("attempt_id") or ""))
        task = workflow_repos.get_task(str(attempt.get("task_id") or "")) if attempt else None
        reconciler = self._reconcilers.get(str(task.get("executor_kind") or "")) if task else None
        outcome: Literal["succeeded", "absent", "unknown"] = "unknown"
        evidence: dict[str, Any] = {}
        if reconciler is not None and attempt is not None and task is not None:
            try:
                outcome = reconciler(task, attempt)
            except Exception:
                _LOG.exception("durable workflow reconciler raised")
        workflow_repos.record_reconciliation(
            str(item.get("attempt_id") or ""), outcome=outcome, evidence=evidence
        )
        workflow_repos.finish_outbox_item(str(item["outbox_id"]), status="delivered")


scheduler = DurableWorkflowScheduler()
