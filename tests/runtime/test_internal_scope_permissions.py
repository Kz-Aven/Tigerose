from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from server.runtime.scope_guard import build_scope_for_intent, classify_scope


class InternalScopePermissionTests(unittest.TestCase):
    def setUp(self):
        self.scope = build_scope_for_intent("one_shot_action")
        self.workspace = tempfile.TemporaryDirectory()
        self.addCleanup(self.workspace.cleanup)

    def classify(self, name, args):
        return classify_scope(self.scope, name, args, cwd=self.workspace.name)

    def test_internal_collaboration_tools_do_not_request_scope_permission(self):
        calls = {
            "todo_write": {"todos": [{"content": "Inspect", "status": "pending"}]},
            "complete_task": {"task_id": "task_1", "result": "Done"},
            "fail_task": {"task_id": "task_1", "reason": "Blocked"},
            "cancel_task": {"task_id": "task_1"},
            "release_task": {"task_id": "task_1"},
            "send_message": {"recipient": "teammate", "content": "Status"},
            "request_plan": {"plan": "Inspect the existing implementation"},
            "request_shutdown": {"name": "teammate"},
            "keep_worktree": {"name": "task-worktree"},
        }
        for name, args in calls.items():
            with self.subTest(tool=name):
                self.assertIsNone(self.classify(name, args))

    def test_archiving_completed_tasks_does_not_request_scope_permission(self):
        self.assertIsNone(self.classify("archive_completed_tasks", {"older_than_days": 7}))

    def test_nonempty_write_and_excel_write_do_not_request_scope_permission(self):
        for name, path, args in (
            ("write_file", "report.txt", {"content": "Report contents"}),
            ("excel_write", "report.xlsx", {"data": [["Report", 1]]}),
        ):
            with self.subTest(tool=name):
                self.assertIsNone(self.classify(name, {"path": path, **args}))

    def test_overwriting_ordinary_file_does_not_request_scope_permission(self):
        target = Path(self.workspace.name) / "report.txt"
        target.write_text("Original", encoding="utf-8")
        self.assertIsNone(
            self.classify("write_file", {"path": str(target), "content": "Updated"})
        )

    def test_edit_file_still_requests_permission(self):
        gate = self.classify(
            "edit_file", {"path": "report.txt", "old_text": "Original", "new_text": "Updated"}
        )
        self.assertIsNotNone(gate)
        self.assertEqual(gate["action"], "ask")

    def test_sensitive_write_targets_still_request_permission(self):
        for name, args in (
            ("write_file", {"content": "Updated"}),
            ("excel_write", {"data": [["Updated"]]}),
        ):
            for prefix in ("server/runtime", "tests/runtime", ".git"):
                with self.subTest(tool=name, prefix=prefix):
                    gate = self.classify(name, {"path": f"{prefix}/report.txt", **args})
                    self.assertIsNotNone(gate)
                    self.assertEqual(gate["action"], "ask")
                    self.assertEqual(gate["reason"], "scope_runtime_edit")


if __name__ == "__main__":
    unittest.main()
