from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from session_store import SessionManager

from server.db import schema, workflow_repos
from server.runtime.executor import ToolResult
from server.runtime.goal import GoalController
from server.runtime.workflow_context import build_context_pack
from server.runtime.workflow_runtime import DurableWorkflowScheduler, ExecutionResult


class WorkflowRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "workflow.db"

        def connect():
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            return conn

        self.connect = connect
        self.schema_patch = patch.object(schema, "get_connection", side_effect=connect)
        self.repo_patch = patch.object(workflow_repos, "get_connection", side_effect=connect)
        self.journal_patch = patch("server.runtime.migration_journal.append_journal_event")
        self.schema_patch.start()
        self.repo_patch.start()
        self.journal_patch.start()
        self.addCleanup(self.schema_patch.stop)
        self.addCleanup(self.repo_patch.stop)
        self.addCleanup(self.journal_patch.stop)
        schema.init_db()

    def _workflow(self) -> dict:
        return workflow_repos.create_workflow(
            kind="test",
            owner_session_id="session_1",
            title="root",
            intent={"request": "test"},
        )

    def _claimed_attempt(self) -> tuple[dict, dict, dict]:
        workflow = self._workflow()
        task = workflow_repos.create_task(
            workflow["workflow_id"], title="write", executor_kind="fake"
        )
        claimed = workflow_repos.claim_next_task("worker-a", executor_kinds={"fake"})
        self.assertIsNotNone(claimed)
        assert claimed is not None
        lease = claimed["lease"]
        attempt = workflow_repos.prepare_attempt(
            task["task_id"],
            lease_token=lease["lease_token"],
            fencing_token=lease["fencing_token"],
            dispatch={"write": "once"},
            idempotency_key="stable-key",
        )
        self.assertIsNotNone(attempt)
        assert attempt is not None
        return workflow, lease, attempt

    def test_expand_migration_is_idempotent_and_adds_pending_action_columns(self):
        schema.init_db()
        schema.init_db()
        conn = self.connect()
        try:
            tables = {
                row[0]
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            }
            self.assertTrue(
                {
                    "workflow_runs",
                    "workflow_tasks",
                    "workflow_attempts",
                    "workflow_events",
                    "workflow_outbox",
                    "workflow_context_packs",
                }.issubset(tables)
            )
            columns = {
                row[1] for row in conn.execute("PRAGMA table_info(pending_actions)").fetchall()
            }
            self.assertTrue({"workflow_id", "task_id", "attempt_id", "decision_type"}.issubset(columns))
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM schema_migrations WHERE migration_id IN (?, ?)",
                             (schema._WORKFLOW_V0_MIGRATION_ID, schema._WORKFLOW_V2_MIGRATION_ID)).fetchone()[0], 2
            )
        finally:
            conn.close()

    def test_legacy_shadow_is_idempotent_and_off_by_default(self):
        from server.runtime import workflow_shadow

        goal = {"goal_id": "goal_1", "condition": "finish", "source": "explicit"}
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AVENT_FF_DURABLE_WORKFLOW_SHADOW_V0", None)
            self.assertFalse(workflow_shadow.activate(goal, owner_session_id="session_1"))
        self.assertEqual(workflow_repos.list_tasks("missing"), [])

        with patch.dict(os.environ, {"AVENT_FF_DURABLE_WORKFLOW_SHADOW_V0": "1"}, clear=False):
            self.assertTrue(workflow_shadow.activate(goal, owner_session_id="session_1"))
            workflow_shadow.record_evidence(
                goal,
                {"evidence_id": "ev_1", "name": "read_file", "outcome": "ok"},
            )
            workflow_shadow.record_evidence(
                goal,
                {"evidence_id": "ev_1", "name": "read_file", "outcome": "ok"},
            )
        events = workflow_repos.list_events(goal["workflow_id"])
        self.assertEqual([event["type"] for event in events].count("GoalEvidence"), 1)
        self.assertEqual(len(workflow_repos.list_tasks(goal["workflow_id"])), 1)

    def test_only_one_worker_can_claim_task(self):
        workflow = self._workflow()
        task = workflow_repos.create_task(workflow["workflow_id"], title="task", executor_kind="fake")
        first = workflow_repos.claim_next_task("worker-a", executor_kinds={"fake"})
        second = workflow_repos.claim_next_task("worker-b", executor_kinds={"fake"})
        self.assertEqual(first["task_id"], task["task_id"])
        self.assertIsNone(second)

    def test_stale_dispatch_recovers_to_in_doubt_without_a_second_provider_call(self):
        workflow, lease, attempt = self._claimed_attempt()
        conn = self.connect()
        try:
            conn.execute(
                "UPDATE workflow_leases SET expires_at = 0 WHERE task_id = ?",
                (attempt["task_id"],),
            )
            conn.execute(
                "UPDATE workflow_outbox SET status = 'dispatching' WHERE attempt_id = ?",
                (attempt["attempt_id"],),
            )
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(workflow_repos.recover_stale_dispatches(), 1)
        self.assertEqual(workflow_repos.get_attempt(attempt["attempt_id"])["status"], "in_doubt")
        self.assertEqual(workflow_repos.get_task(attempt["task_id"])["status"], "reconciling")
        self.assertNotEqual(workflow_repos.next_outbox_item()["kind"], "provider_dispatch")

    def test_pre_dispatch_crash_reuses_frozen_attempt_and_idempotency_key(self):
        workflow, lease, attempt = self._claimed_attempt()
        conn = self.connect()
        try:
            conn.execute(
                "UPDATE workflow_leases SET expires_at = 0 WHERE task_id = ?",
                (attempt["task_id"],),
            )
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(workflow_repos.expire_leases(), 1)
        replacement = workflow_repos.claim_next_task("worker-b", executor_kinds={"fake"})
        resumed = workflow_repos.prepare_attempt(
            attempt["task_id"],
            lease_token=replacement["lease"]["lease_token"],
            fencing_token=replacement["lease"]["fencing_token"],
            dispatch={"write": "ignored-new-payload"},
            idempotency_key="must-not-be-used",
        )
        self.assertEqual(resumed["attempt_id"], attempt["attempt_id"])
        self.assertEqual(resumed["idempotency_key"], "stable-key")
        self.assertEqual(len(workflow_repos.list_attempts(attempt["task_id"])), 1)

    def test_unknown_reconciliation_requires_explicit_decision(self):
        workflow, lease, attempt = self._claimed_attempt()
        self.assertTrue(
            workflow_repos.mark_attempt_in_doubt(
                attempt["attempt_id"],
                lease_token=lease["lease_token"],
                fencing_token=lease["fencing_token"],
            )
        )
        outcome = workflow_repos.record_reconciliation(
            attempt["attempt_id"], outcome="unknown", evidence={"provider": "unavailable"}
        )
        self.assertEqual(outcome["status"], "waiting_for_user")
        self.assertEqual(workflow_repos.get_task(attempt["task_id"])["status"], "waiting_for_user")
        self.assertTrue(
            workflow_repos.decide_pending_action(
                outcome["pending_action_id"],
                owner_session_id="session_1",
                decision="retry_confirmed_absent",
                actor="tester",
            )
        )
        self.assertEqual(workflow_repos.get_task(attempt["task_id"])["status"], "ready")
        self.assertEqual(workflow_repos.get_attempt(attempt["attempt_id"])["status"], "reconciled_absent")

    def test_stale_worker_cannot_commit_after_fencing_token_changes(self):
        workflow, lease, attempt = self._claimed_attempt()
        conn = self.connect()
        try:
            conn.execute(
                "UPDATE workflow_leases SET expires_at = 0 WHERE task_id = ?",
                (attempt["task_id"],),
            )
            conn.commit()
        finally:
            conn.close()
        workflow_repos.expire_leases()
        replacement = workflow_repos.claim_next_task("worker-b", executor_kinds={"fake"})
        self.assertGreater(replacement["lease"]["fencing_token"], lease["fencing_token"])
        self.assertFalse(
            workflow_repos.complete_attempt(
                attempt["attempt_id"],
                lease_token=lease["lease_token"],
                fencing_token=lease["fencing_token"],
            )
        )
        self.assertEqual(workflow_repos.get_task(attempt["task_id"])["status"], "running")

    def test_context_pack_uses_committed_event_watermark(self):
        workflow = self._workflow()
        task = workflow_repos.create_task(workflow["workflow_id"], title="research", executor_kind="fake")
        workflow_repos.record_artifact(task["task_id"], kind="note", uri="artifact://note", sha256="abc")
        pack = build_context_pack(task["task_id"])
        latest_event = workflow_repos.list_events(workflow["workflow_id"])[-1]
        self.assertLess(pack["source_event_seq"], latest_event["sequence"])
        stored = workflow_repos.latest_context_pack(task["task_id"])
        self.assertEqual(stored["source_event_seq"], pack["source_event_seq"])
        self.assertEqual(stored["artifact_refs"][0]["sha256"], "abc")

    def test_scheduler_executes_registered_adapter_once(self):
        workflow = self._workflow()
        task = workflow_repos.create_task(workflow["workflow_id"], title="send", executor_kind="fake")
        calls: list[str] = []
        worker = DurableWorkflowScheduler(owner_id="test-worker")
        worker.register_executor(
            "fake",
            lambda current_task, attempt: (
                calls.append(attempt["idempotency_key"])
                or ExecutionResult("succeeded", receipt={"receipt_id": "r1"})
            ),
        )
        with patch.dict(
            os.environ,
            {
                "AVENT_FF_DURABLE_WORKFLOW_SHADOW_V0": "1",
                "AVENT_FF_DURABLE_WORKFLOW_V1": "1",
            },
            clear=False,
        ), patch("server.runtime.run_transcript.append_run_event"):
            worker.tick()
            worker.tick()
        self.assertEqual(len(calls), 1)
        self.assertEqual(workflow_repos.get_task(task["task_id"])["status"], "completed")

    def test_budget_reservation_is_atomic_across_repository_instances(self):
        conn = self.connect()
        try:
            conn.execute(
                """INSERT INTO runtime_budgets
                   (root_budget_id, profile, limits_json, consumed_json, reserved_json,
                    active_execution_ms, version, created_at, updated_at)
                   VALUES ('budget_1', 'durable_goal', '{\"hard_tool_calls\":1}', '{}', '{}', 0, 1, 0, 0)"""
            )
            conn.commit()
        finally:
            conn.close()
        self.assertTrue(workflow_repos.reserve_budget("budget_1", "reserve_1", "tool_call"))
        self.assertFalse(workflow_repos.reserve_budget("budget_1", "reserve_2", "tool_call"))
        self.assertTrue(workflow_repos.settle_budget_reservation("reserve_1", commit=True))
        self.assertFalse(workflow_repos.reserve_budget("budget_1", "reserve_3", "tool_call"))

    def test_goal_projection_reads_durable_evidence_and_writes_terminal_state(self):
        root = Path(self.tmp.name) / "sessions-projection"
        manager = SessionManager(root, {"session": {"persist_dir": ".sessions"}})
        state = manager.create_session("assistant_dm", "assistant:test", title="test")
        controller = GoalController(
            state=state,
            session_manager=manager,
            board_scope="assistant:test",
            run_id="run_1",
            user_request="finish report",
            evaluator=lambda payload: SimpleNamespace(completed=True, reason="done", missing_evidence=[], next_action="", model_profile_id="test"),
        )
        with patch.dict(
            os.environ,
            {
                "AVENT_FF_DURABLE_WORKFLOW_SHADOW_V0": "1",
                "AVENT_FF_DURABLE_WORKFLOW_V1": "1",
                "AVENT_FF_GOAL_WORKFLOW_PROJECTION_V1": "1",
            },
            clear=False,
        ), patch("server.runtime.run_transcript.append_run_event"):
            controller.activate("finish report", source="explicit")
            controller.before_tool("mcp__provider__write", {}, "call_1")
            controller.after_tool(
                ToolResult(
                    "timeout",
                    "timeout",
                    {"tool_call_id": "call_1", "tool_name": "mcp__provider__write", "tool_kind": "mutation"},
                )
            )
            controller.goal["evidence"] = []
            self.assertEqual(controller.unresolved_tool_failures(), ["mcp__provider__write [timeout]"])
            controller.goal["evidence"] = []
            controller.goal["status"] = "achieved"
            from server.runtime import workflow_shadow

            workflow_shadow.terminal(controller.goal, reason="test")
        projection = workflow_repos.workflow_projection(controller.goal["workflow_id"])
        self.assertEqual(projection["status"], "completed")
        self.assertEqual(projection["tasks"][0]["status"], "completed")

    def test_goal_shadow_hook_does_not_change_goal_semantics_when_enabled(self):
        root = Path(self.tmp.name) / "sessions"
        manager = SessionManager(root, {"session": {"persist_dir": ".sessions"}})
        state = manager.create_session("assistant_dm", "assistant:test", title="test")
        controller = GoalController(
            state=state,
            session_manager=manager,
            board_scope="assistant:test",
            run_id="run_1",
            user_request="finish report",
            evaluator=lambda payload: SimpleNamespace(completed=True, reason="done", missing_evidence=[], next_action="", model_profile_id="test"),
        )
        with patch.dict(os.environ, {"AVENT_FF_DURABLE_WORKFLOW_SHADOW_V0": "1"}, clear=False), patch(
            "server.runtime.run_transcript.append_run_event"
        ):
            controller.activate("finish report", source="explicit")
            controller.before_tool("read_file", {}, "call_1")
            controller.after_tool(ToolResult("ok", "ok", {"tool_call_id": "call_1", "tool_name": "read_file"}))
        self.assertEqual(controller.goal["status"], "active")
        self.assertTrue(controller.goal.get("workflow_id"))


if __name__ == "__main__":
    unittest.main()
