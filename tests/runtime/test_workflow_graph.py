from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from server.db import schema, workflow_repos


class WorkflowGraphTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        path = Path(self.tmp.name) / "workflow.db"

        def connect():
            conn = sqlite3.connect(path)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            return conn

        self.connect = connect
        self.schema_patch = patch.object(schema, "get_connection", side_effect=connect)
        self.repo_patch = patch.object(workflow_repos, "get_connection", side_effect=connect)
        self.schema_patch.start()
        self.repo_patch.start()
        self.addCleanup(self.schema_patch.stop)
        self.addCleanup(self.repo_patch.stop)
        schema.init_db()

    def _workflow(self):
        return workflow_repos.create_workflow(kind="test", owner_session_id="scope", title="workflow")

    def _attempt(self, task_id: str):
        claim = workflow_repos.claim_next_task("worker", executor_kinds={"fake"})
        self.assertIsNotNone(claim)
        assert claim is not None
        lease = claim["lease"]
        attempt = workflow_repos.prepare_attempt(task_id, lease_token=lease["lease_token"], fencing_token=lease["fencing_token"], dispatch={})
        self.assertIsNotNone(attempt)
        return lease, attempt

    def test_hard_failure_blocks_downstream_while_soft_dependency_does_not(self):
        workflow = self._workflow()
        failed = workflow_repos.create_task(workflow["workflow_id"], title="failed", executor_kind="fake")
        hard = workflow_repos.create_task(workflow["workflow_id"], title="hard", executor_kind="fake", depends_on=[(failed["task_id"], "hard")])
        soft = workflow_repos.create_task(workflow["workflow_id"], title="soft", executor_kind="fake", depends_on=[(failed["task_id"], "soft")])
        lease, attempt = self._attempt(failed["task_id"])
        self.assertTrue(workflow_repos.fail_attempt(attempt["attempt_id"], lease_token=lease["lease_token"], fencing_token=lease["fencing_token"], error_code="broken"))
        self.assertEqual(workflow_repos.reap_dependency_blocks(), 1)
        self.assertEqual(workflow_repos.get_task(hard["task_id"])["status"], "blocked_by_dependency")
        self.assertEqual(workflow_repos.get_task(soft["task_id"])["status"], "ready")

    def test_retry_policy_is_bounded_and_revision_fences_downstream(self):
        workflow = self._workflow()
        retry = workflow_repos.create_task(workflow["workflow_id"], title="retry", executor_kind="fake", retry_policy={"max_attempts": 2, "base_delay_s": 0.1, "retryable_codes": ["network"]})
        child = workflow_repos.create_task(workflow["workflow_id"], title="child", executor_kind="fake", depends_on=[(retry["task_id"], "hard")])
        lease, attempt = self._attempt(retry["task_id"])
        self.assertTrue(workflow_repos.schedule_retry(attempt["attempt_id"], lease_token=lease["lease_token"], fencing_token=lease["fencing_token"], error_code="network"))
        pending = workflow_repos.get_task(retry["task_id"])
        self.assertEqual(pending["status"], "retry_wait")
        claim = workflow_repos.claim_next_task("worker-2", now=float(pending["next_run_at"]) + 1, executor_kinds={"fake"})
        self.assertIsNotNone(claim)
        assert claim is not None
        second = workflow_repos.prepare_attempt(retry["task_id"], lease_token=claim["lease"]["lease_token"], fencing_token=claim["lease"]["fencing_token"], dispatch={})
        self.assertTrue(workflow_repos.schedule_retry(second["attempt_id"], lease_token=claim["lease"]["lease_token"], fencing_token=claim["lease"]["fencing_token"], error_code="network"))
        self.assertEqual(workflow_repos.get_task(retry["task_id"])["status"], "failed")
        revised = workflow_repos.revise_task(retry["task_id"], intent={"changed": True}, actor="tester")
        self.assertEqual(revised["revision"], 2)
        self.assertEqual(workflow_repos.get_task(child["task_id"])["revision"], 2)

    def test_milestone_requires_artifact_acceptance(self):
        workflow = self._workflow()
        task = workflow_repos.create_task(workflow["workflow_id"], title="deliver", executor_kind="fake", acceptance_policy="artifact_required")
        milestone = workflow_repos.create_milestone(workflow["workflow_id"], title="release", task_ids=[task["task_id"]])
        lease, attempt = self._attempt(task["task_id"])
        self.assertTrue(workflow_repos.complete_attempt(attempt["attempt_id"], lease_token=lease["lease_token"], fencing_token=lease["fencing_token"]))
        self.assertEqual(workflow_repos.get_milestone(milestone["milestone_id"])["status"], "pending")
        workflow_repos.record_artifact(task["task_id"], kind="report", uri="artifact://report", sha256="sha")
        self.assertEqual(workflow_repos.get_milestone(milestone["milestone_id"])["status"], "accepted")
