from __future__ import annotations

import unittest
from pathlib import Path

from server.runtime.executor import ToolExecutor
from server.runtime.mcp import MCPCallResult, MCPClient


class MCPOutcomeTests(unittest.TestCase):
    def test_unknown_tool_returns_error(self) -> None:
        client = MCPClient("test")
        executor = ToolExecutor(cwd=Path(".").resolve(), mcp_clients={"test": client})
        result = executor.execute("mcp__test__missing", {})
        self.assertEqual(result.outcome, "error")
        self.assertIn("unknown tool", result.content)

    def test_is_error_true_returns_error(self) -> None:
        result = ToolExecutor._normalize(  # noqa: SLF001
            MCPCallResult(is_error=True, content='MCP error: {"isError": true}'),
            {},
        )
        self.assertEqual(result.outcome, "error")

    def test_normal_text_success_returns_ok(self) -> None:
        client = MCPClient("test")
        client.register(
            [{"name": "ping", "description": "ping", "inputSchema": {"type": "object"}}],
            {"ping": lambda: "pong"},
        )
        executor = ToolExecutor(cwd=Path(".").resolve(), mcp_clients={"test": client})
        result = executor.execute("mcp__test__ping", {})
        self.assertEqual(result.outcome, "ok")
        self.assertEqual(result.content, "pong")

    def test_normalize_mcp_error_string(self) -> None:
        result = ToolExecutor._normalize("MCP error: x", {})  # noqa: SLF001
        self.assertEqual(result.outcome, "error")
        self.assertEqual(result.content, "MCP error: x")

    def test_normalize_is_error_json_in_string(self) -> None:
        result = ToolExecutor._normalize('{"isError": true, "content": []}', {})  # noqa: SLF001
        self.assertEqual(result.outcome, "error")

    def test_dingtalk_receipt_from_structured_content(self) -> None:
        result = ToolExecutor._normalize(  # noqa: SLF001
            MCPCallResult(
                is_error=False,
                content="ok",
                structured_content={
                    "processQueryKey": "pq_123",
                    "openConversationId": "oc_group_1",
                },
            ),
            {"tool_name": "mcp__dingtalk__send"},
        )
        self.assertEqual(result.outcome, "ok")
        receipt = result.metadata.get("acceptance_receipt")
        self.assertIsInstance(receipt, dict)
        self.assertEqual(receipt["receipt_id"], "pq_123")
        self.assertEqual(receipt["target_id"], "oc_group_1")

    def test_success_text_does_not_invent_receipt(self) -> None:
        result = ToolExecutor._normalize("success", {})  # noqa: SLF001
        self.assertEqual(result.outcome, "ok")
        self.assertNotIn("acceptance_receipt", result.metadata)


if __name__ == "__main__":
    unittest.main()
