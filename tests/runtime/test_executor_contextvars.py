"""Regression: permission contextvars must reach dangerous-handler worker threads.

Root cause: ToolExecutor._run_worker executed handlers in a ThreadPoolExecutor
worker thread. `allow_external` / `allow_dangerous_bash` are contextvars set in
the loop thread; without contextvars.copy_context() the worker saw defaults and
permission-granted external-path edits failed with "path escapes workspace"
even after the permission gate emitted PermissionGranted.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from server.runtime.executor import ToolExecutor
from server.runtime.tools import files as file_tools


class ExecutorContextVarsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.outside_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.outside_tmp.cleanup)
        self.outside = Path(self.outside_tmp.name)

    def _make_executor(self) -> ToolExecutor:
        return ToolExecutor(
            cwd=self.root,
            dangerous_handlers={
                "probe_external": lambda path: file_tools.read_file(
                    self.root, path
                )
            },
        )

    def test_external_read_allowed_in_worker_thread(self):
        sample = self.outside / "note.txt"
        sample.write_text("hello external", encoding="utf-8")
        executor = self._make_executor()
        token = file_tools.allow_external(True)
        try:
            result = executor.execute("probe_external", {"path": str(sample)})
        finally:
            file_tools.reset_allow_external(token)
        self.assertEqual(result.outcome, "ok")
        self.assertIn("hello external", result.content)

    def test_external_read_denied_without_permission(self):
        sample = self.outside / "secret.txt"
        sample.write_text("secret", encoding="utf-8")
        executor = self._make_executor()
        result = executor.execute("probe_external", {"path": str(sample)})
        self.assertEqual(result.outcome, "denied")
        self.assertIn("path escapes workspace", result.content)


if __name__ == "__main__":
    unittest.main()
