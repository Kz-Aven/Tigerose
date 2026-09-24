from __future__ import annotations

import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from server.db import schema, workflow_repos
from server.runtime.workflow_adapters import code_worktree


class CodeWorkflowAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        database = self.root / "workflow.db"

        def connect():
            conn = sqlite3.connect(database)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            return conn

        self.schema_patch = patch.object(schema, "get_connection", side_effect=connect)
        self.repo_patch = patch.object(workflow_repos, "get_connection", side_effect=connect)
        self.data_patch = patch.object(code_worktree, "data_root", return_value=self.root / "runtime")
        self.ensure_patch = patch.object(code_worktree, "ensure_data_dirs", return_value=self.root / "runtime")
        self.schema_patch.start()
        self.repo_patch.start()
        self.data_patch.start()
        self.ensure_patch.start()
        self.addCleanup(self.schema_patch.stop)
        self.addCleanup(self.repo_patch.stop)
        self.addCleanup(self.data_patch.stop)
        self.addCleanup(self.ensure_patch.stop)
        schema.init_db()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self._git("init")
        (self.repo / "README.md").write_text("base\n", encoding="utf-8")
        self._git("add", "README.md")
        self._git("-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "base")

    def _git(self, *args: str) -> None:
        subprocess.run(["git", *args], cwd=self.repo, check=True, capture_output=True, text=True)

    def _task(self, merge_target: str = "main") -> dict:
        workflow = workflow_repos.create_workflow(kind="code", owner_session_id="scope", title="code")
        return workflow_repos.create_task(workflow["workflow_id"], title="verify", executor_kind="local_git_worktree", workspace_path=str(self.repo), merge_target=merge_target)

    def test_argv_commands_run_in_isolated_worktree_and_create_manual_gate(self):
        task = self._task()
        result = code_worktree.execute(task, {"dispatch": {"commands": [["git", "status", "--porcelain"]]}})
        self.assertEqual(result.outcome, "succeeded")
        self.assertNotEqual(result.receipt["worktree_path"], str(self.repo))
        self.assertTrue(Path(result.receipt["worktree_path"]).is_dir())
        self.assertEqual(len(workflow_repos.list_artifacts(task["task_id"])), 2)
        claim = workflow_repos.claim_next_task("worker", executor_kinds={"local_git_worktree"})
        assert claim is not None
        attempt = workflow_repos.prepare_attempt(task["task_id"], lease_token=claim["lease"]["lease_token"], fencing_token=claim["lease"]["fencing_token"], dispatch={})
        assert attempt is not None
        self.assertTrue(workflow_repos.complete_attempt(attempt["attempt_id"], lease_token=claim["lease"]["lease_token"], fencing_token=claim["lease"]["fencing_token"], receipt=result.receipt))
        gate = workflow_repos.mark_task_waiting_for_merge(task["task_id"], merge_target="main", receipt=result.receipt)
        self.assertEqual(gate["status"], "pending")
        self.assertTrue(workflow_repos.decide_merge_gate(gate["gate_id"], owner_session_id="scope", approved=True, actor="reviewer"))
        self.assertEqual(workflow_repos.get_task(task["task_id"])["status"], "completed")

    def test_shell_string_and_failed_command_are_not_accepted(self):
        task = self._task(merge_target="")
        self.assertEqual(code_worktree.execute(task, {"dispatch": {"commands": ["git status"]}}).outcome, "failed")
        result = code_worktree.execute(task, {"dispatch": {"commands": [["git", "rev-parse", "missing-ref"]]}})
        self.assertEqual(result.outcome, "failed")
        logs = workflow_repos.list_artifacts(task["task_id"])
        self.assertEqual(logs[0]["metadata"]["exit_code"], 128)
