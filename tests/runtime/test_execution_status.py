from __future__ import annotations

import unittest

from server.runtime.run_outcome import map_loop_termination
from server.runtime.turn import _collect_completion_evidence, _execution_status


class ExecutionStatusTests(unittest.TestCase):
    def test_normal_stop_completes_without_domain_acceptance(self):
        outcome = map_loop_termination(
            termination_reason="normal_stop",
            intent="one_shot_action",
        )
        self.assertEqual(outcome.status, "completed")
        self.assertEqual(outcome.reason_code, "normal_stop")
        self.assertEqual(_execution_status(outcome.status), "completed")

    def test_written_file_and_post_write_check_are_verified(self):
        evidence = _collect_completion_evidence(
            [
                {
                    "tool": "write_file",
                    "args": {"path": "results/report.md"},
                    "outcome": "ok",
                    "metadata": {"tool_effect": "write"},
                },
                {
                    "tool": "bash",
                    "args": {"command": "test -s results/report.md"},
                    "outcome": "ok",
                    "metadata": {"tool_effect": "read"},
                },
            ],
            None,
        )
        self.assertEqual(evidence["status"], "verified")
        self.assertEqual(evidence["write_paths"], ["results/report.md"])
        self.assertEqual(evidence["post_write_checks"], 1)

    def test_written_file_without_check_is_observed_not_failed(self):
        evidence = _collect_completion_evidence(
            [
                {
                    "tool": "write_file",
                    "args": {"path": "results/report.md"},
                    "outcome": "ok",
                    "metadata": {"tool_effect": "write"},
                }
            ],
            None,
        )
        self.assertEqual(evidence["status"], "observed")


if __name__ == "__main__":
    unittest.main()
