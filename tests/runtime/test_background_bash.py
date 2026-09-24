from __future__ import annotations

import shlex
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from server.runtime import background_bash as jobs
from server.runtime.tools import files


def python_command(code):
    return shlex.quote(sys.executable) + " -c " + shlex.quote(code)


class BackgroundBashTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ctx = SimpleNamespace(scope_key="mesh:test:A", template_id="A", cwd=Path(self.tmp.name))
        self.addCleanup(jobs.cleanup_scope, self.ctx.scope_key, self.ctx.template_id)
        timeout = patch.object(jobs, "_terminal_timeout", return_value=10)
        timeout.start()
        self.addCleanup(timeout.stop)

    def test_start_returns_before_process_finishes_and_wait_collects_exit(self):
        started = time.monotonic()
        result = jobs.start(self.ctx, python_command("import time; time.sleep(1.2); print('finished')"))
        self.assertLess(time.monotonic() - started, 0.8)
        self.assertEqual(result["status"], "running")
        final = jobs.wait(self.ctx, result["job_id"], timeout_s=3)
        self.assertEqual(final["status"], "completed")
        self.assertEqual(final["exit_code"], 0)
        self.assertIn("finished", final["output"])

    def test_large_output_cannot_block_on_an_unread_pipe(self):
        result = jobs.start(self.ctx, python_command("import sys; sys.stdout.write('x' * 200000); sys.stderr.write('tail')"))
        final = jobs.wait(self.ctx, result["job_id"], timeout_s=3)
        self.assertEqual(final["status"], "completed")
        self.assertTrue(final["output_truncated"])
        self.assertLessEqual(len(final["output"]), 50000)
        self.assertTrue(final["output"].endswith("tail"))

    def test_other_assistant_or_scope_cannot_read_wait_or_cancel(self):
        result = jobs.start(self.ctx, python_command("import time; time.sleep(10)"))
        impostors = [SimpleNamespace(scope_key=self.ctx.scope_key, template_id="B"),
                     SimpleNamespace(scope_key="mesh:other:A", template_id="A")]
        for impostor in impostors:
            for operation in (jobs.status, jobs.wait, jobs.cancel):
                with self.assertRaises(PermissionError):
                    operation(impostor, result["job_id"])
        self.assertEqual(jobs.status(self.ctx, result["job_id"])["status"], "running")

    def test_cancel_stops_the_process_and_is_idempotent(self):
        result = jobs.start(self.ctx, python_command("import time; time.sleep(10)"))
        final = jobs.cancel(self.ctx, result["job_id"])
        self.assertEqual(final["status"], "cancelled")
        self.assertIsNotNone(final["exit_code"])
        self.assertEqual(jobs.cancel(self.ctx, result["job_id"])["status"], "cancelled")

    def test_timeout_terminates_without_polling(self):
        with patch.object(jobs, "_terminal_timeout", return_value=0.15):
            result = jobs.start(self.ctx, python_command("import time; time.sleep(10)"))
        final = jobs.wait(self.ctx, result["job_id"], timeout_s=3)
        self.assertEqual(final["status"], "timeout")
        self.assertIsNotNone(final["exit_code"])

    def test_dangerous_gate_is_checked_before_thread_dispatch(self):
        command = "printf '%s' 'sudo is only test text'"
        with patch.object(jobs.subprocess, "Popen") as start_process:
            with self.assertRaises(PermissionError):
                jobs.start(self.ctx, command)
            start_process.assert_not_called()
        token = files.allow_dangerous_bash(True)
        try:
            result = jobs.start(self.ctx, command)
        finally:
            files.reset_allow_dangerous_bash(token)
        final = jobs.wait(self.ctx, result["job_id"], timeout_s=3)
        self.assertEqual(final["status"], "completed")
        self.assertEqual(final["output"], "sudo is only test text")

    def test_cleanup_stops_jobs_and_removes_output_files(self):
        result = jobs.start(self.ctx, python_command("import time; time.sleep(10)"))
        job = jobs._jobs[result["job_id"]]
        self.assertTrue(job.output_path.exists())
        jobs.cleanup_scope(self.ctx.scope_key, self.ctx.template_id)
        self.assertFalse(job.output_path.exists())
        self.assertIsNotNone(job.process.poll())
        with self.assertRaises(KeyError):
            jobs.status(self.ctx, result["job_id"])


if __name__ == "__main__":
    unittest.main()
