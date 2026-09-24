from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from server.runtime.acceptance import evaluate_acceptance
from server.runtime.attempt_ledger import AttemptLedger
from server.runtime.objectives import build_objective_from_message, payload_digest, resolve_target
from server.runtime.run_outcome import map_loop_termination


class AttemptLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ledger = AttemptLedger()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runs_dir = Path(self.tmp.name)

    def _objective(self, *, target_id: str = "group_a", digest: str | None = None):
        obj = build_objective_from_message(
            "给【钉钉日程测试群】发消息说【今天上午 11 点的会议取消】",
            "one_shot_action",
            domain="dingtalk.message",
            operation="send",
        )
        obj = resolve_target(obj, target_id)
        if digest is not None:
            data = obj.to_dict()
            data["expected_payload_digest"] = digest
            from server.runtime.objectives import Objective

            obj = Objective.from_dict(data)
        self.ledger.objectives[obj.objective_id] = obj.to_dict()
        return obj

    def test_failure_not_lost_after_many_events(self) -> None:
        obj = self._objective()
        failed = self.ledger.create_attempt(
            objective_id=obj.objective_id,
            tool_name="send_robot_group_message",
            effect="write",
            goal_role="terminal_action",
            resolved_target_id=obj.resolved_target_id or "",
            attempted_payload_digest=obj.expected_payload_digest,
            objective_revision=obj.revision,
        )
        self.ledger.mark_failed(failed.attempt_id)

        for i in range(100):
            query = self.ledger.create_attempt(
                objective_id=obj.objective_id,
                tool_name="search_groups_by_keyword",
                effect="read",
                goal_role="query",
                sequence=i + 1,
            )
            self.ledger.mark_succeeded(query.attempt_id)
            self.ledger.append_event(
                "run_1",
                {"attempt_id": query.attempt_id, "type": "attempt_succeeded"},
                self.runs_dir,
            )

        blocking = self.ledger.blocking_attempts(obj.objective_id)
        self.assertEqual(len(blocking), 1)
        self.assertEqual(blocking[0].attempt_id, failed.attempt_id)
        self.assertEqual(blocking[0].status, "failed")

    def test_supersede_same_objective_target_digest(self) -> None:
        obj = self._objective()
        digest = obj.expected_payload_digest

        old = self.ledger.create_attempt(
            objective_id=obj.objective_id,
            tool_name="send_robot_group_message",
            effect="write",
            goal_role="terminal_action",
            resolved_target_id=obj.resolved_target_id or "",
            attempted_payload_digest=digest,
            objective_revision=obj.revision,
        )
        self.ledger.mark_failed(old.attempt_id)

        new = self.ledger.create_attempt(
            objective_id=obj.objective_id,
            tool_name="send_robot_group_message",
            effect="write",
            goal_role="terminal_action",
            resolved_target_id=obj.resolved_target_id or "",
            attempted_payload_digest=digest,
            parent_attempt_id=old.attempt_id,
            objective_revision=obj.revision,
        )
        self.ledger.mark_succeeded(
            new.attempt_id,
            acceptance_receipt={
                "receipt_id": "rq_123",
                "target_id": obj.resolved_target_id,
            },
        )

        superseded = self.ledger.supersede(old.attempt_id, new.attempt_id)
        self.assertEqual(superseded.status, "superseded")
        self.assertEqual(self.ledger.blocking_attempts(obj.objective_id), [])

    def test_different_resource_cannot_supersede(self) -> None:
        obj = self._objective(target_id="group_a")
        digest = obj.expected_payload_digest

        old = self.ledger.create_attempt(
            objective_id=obj.objective_id,
            tool_name="send_robot_group_message",
            effect="write",
            goal_role="terminal_action",
            resolved_target_id="group_a",
            attempted_payload_digest=digest,
            objective_revision=obj.revision,
        )
        self.ledger.mark_failed(old.attempt_id)

        new = self.ledger.create_attempt(
            objective_id=obj.objective_id,
            tool_name="send_robot_group_message",
            effect="write",
            goal_role="terminal_action",
            resolved_target_id="group_b",
            attempted_payload_digest=digest,
            objective_revision=obj.revision,
        )
        self.ledger.mark_succeeded(new.attempt_id)

        with self.assertRaises(ValueError):
            self.ledger.supersede(old.attempt_id, new.attempt_id)

        blocking = self.ledger.blocking_attempts(obj.objective_id)
        self.assertEqual(len(blocking), 1)
        self.assertEqual(blocking[0].attempt_id, old.attempt_id)

    def test_query_failure_never_blocking(self) -> None:
        obj = self._objective()
        query = self.ledger.create_attempt(
            objective_id=obj.objective_id,
            tool_name="search_groups_by_keyword",
            effect="read",
            goal_role="query",
        )
        self.ledger.mark_failed(query.attempt_id)

        self.assertEqual(self.ledger.blocking_attempts(obj.objective_id), [])

    def test_in_doubt_is_scoped_to_its_own_objective(self) -> None:
        first = self._objective(target_id="calendar_a")
        second = self._objective(target_id="calendar_b")
        attempt = self.ledger.create_attempt(
            objective_id=first.objective_id,
            tool_name="dingtalk_cli",
            effect="write",
            goal_role="terminal_action",
        )
        self.ledger.mark_in_doubt(attempt.attempt_id, receipt={"operation_id": "dingtalk:create"})
        self.assertEqual(len(self.ledger.blocking_attempts(first.objective_id)), 1)
        self.assertEqual(self.ledger.blocking_attempts(second.objective_id), [])

    def test_message_sent_acceptance_completes(self) -> None:
        obj = self._objective(target_id="oc_group_1")
        digest = obj.expected_payload_digest

        failed = self.ledger.create_attempt(
            objective_id=obj.objective_id,
            tool_name="send_robot_group_message",
            effect="write",
            goal_role="terminal_action",
            resolved_target_id=obj.resolved_target_id or "",
            attempted_payload_digest=digest,
            objective_revision=obj.revision,
            idempotency_key=obj.idempotency_key,
        )
        self.ledger.mark_failed(failed.attempt_id)

        success = self.ledger.create_attempt(
            objective_id=obj.objective_id,
            tool_name="send_robot_group_message",
            effect="write",
            goal_role="terminal_action",
            resolved_target_id=obj.resolved_target_id or "",
            attempted_payload_digest=digest,
            parent_attempt_id=failed.attempt_id,
            objective_revision=obj.revision,
            idempotency_key=obj.idempotency_key,
        )
        self.ledger.mark_succeeded(
            success.attempt_id,
            acceptance_receipt={
                "receipt_id": "processQueryKey_abc",
                "target_id": "oc_group_1",
            },
        )
        self.ledger.supersede(failed.attempt_id, success.attempt_id)

        attempts = [
            self.ledger._get_attempt(aid)  # noqa: SLF001
            for aid in (failed.attempt_id, success.attempt_id)
        ]
        decision = evaluate_acceptance(
            "message_sent",
            intent="one_shot_action",
            objective=obj,
            attempts=attempts,
        )
        self.assertEqual(decision.status, "accepted")
        self.assertIn("processQueryKey_abc", decision.receipt_ids)

        outcome = map_loop_termination(
            termination_reason="accepted",
            objective_id=obj.objective_id,
            acceptance=decision,
            intent="one_shot_action",
        )
        self.assertEqual(outcome.status, "completed")

    def test_payload_digest_format(self) -> None:
        digest = payload_digest({"message_text": "hello", "target_display_name": "群A"})
        self.assertTrue(digest.startswith("sha256:"))

    def test_message_sent_rejects_wrong_target_and_digest_with_same_idempotency(self) -> None:
        obj = self._objective(target_id="oc_group_1")
        shared_idempotency = obj.idempotency_key
        wrong_digest = payload_digest({"message_text": "wrong payload"})

        attempt = self.ledger.create_attempt(
            objective_id=obj.objective_id,
            tool_name="send_robot_group_message",
            effect="write",
            goal_role="terminal_action",
            resolved_target_id="oc_group_wrong",
            attempted_payload_digest=wrong_digest,
            objective_revision=obj.revision,
            idempotency_key=shared_idempotency,
        )
        self.ledger.mark_succeeded(
            attempt.attempt_id,
            acceptance_receipt={
                "receipt_id": "processQueryKey_abc",
                "target_id": "oc_group_wrong",
            },
        )

        decision = evaluate_acceptance(
            "message_sent",
            intent="one_shot_action",
            objective=obj,
            attempts=[self.ledger._get_attempt(attempt.attempt_id)],  # noqa: SLF001
        )
        self.assertNotEqual(decision.status, "accepted")
        self.assertIn(decision.status, {"insufficient", "rejected"})


if __name__ == "__main__":
    unittest.main()
