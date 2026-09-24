from __future__ import annotations

import hashlib
import json
import unittest
from unittest.mock import patch

from server.runtime.executor import ToolResult
from server.runtime.workflow_adapters import dingtalk_message


def _intent(argv: list[str]) -> dict:
    target = "cid"
    return {"argv": argv, "target_id": target, "payload_digest": hashlib.sha256(json.dumps({"argv": argv, "target_id": target}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()}


class DingTalkWorkflowAdapterTests(unittest.TestCase):
    def test_success_needs_parsed_receipt(self):
        argv = ["chat", "message", "send", "--conversation-id", "cid", "--text", "hi"]
        with patch.object(dingtalk_message.dingtalk_cli, "run_cli", return_value=ToolResult("ok", "ok", {"acceptance_receipt": {"receipt_id": "m1"}})):
            result = dingtalk_message.execute({}, {"dispatch": _intent(argv)})
        self.assertEqual(result.outcome, "succeeded")

    def test_timeout_and_missing_reconciler_query_stay_in_doubt(self):
        argv = ["chat", "message", "send", "--conversation-id", "cid", "--text", "hi"]
        with patch.object(dingtalk_message.dingtalk_cli, "run_cli", return_value=ToolResult("unknown", "in_doubt", {})):
            result = dingtalk_message.execute({}, {"dispatch": _intent(argv)})
        self.assertEqual(result.outcome, "unknown")
        self.assertEqual(dingtalk_message.reconcile({}, {"dispatch": _intent(argv)}), "unknown")

    def test_matching_query_receipt_confirms_without_resend(self):
        argv = ["chat", "message", "send", "--conversation-id", "cid", "--text", "hi"]
        intent = _intent(argv)
        intent["reconcile_argv"] = ["chat", "message", "get", "--conversation-id", "cid"]
        with patch.object(dingtalk_message.dingtalk_cli, "run_cli", return_value=ToolResult('{"messageId":"m1"}', "ok", {})):
            self.assertEqual(dingtalk_message.reconcile({}, {"dispatch": intent}), "succeeded")

    def test_digest_mismatch_never_calls_provider(self):
        argv = ["chat", "message", "send", "--conversation-id", "cid", "--text", "hi"]
        intent = _intent(argv)
        intent["payload_digest"] = "wrong"
        with patch.object(dingtalk_message.dingtalk_cli, "run_cli") as call:
            result = dingtalk_message.execute({}, {"dispatch": intent})
        self.assertEqual(result.outcome, "failed")
        call.assert_not_called()
