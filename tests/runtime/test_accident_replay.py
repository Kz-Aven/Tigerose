"""Accident replay: one-shot DingTalk send must not activate Goal or expand scope."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from server.runtime.executor import ToolExecutor
from server.runtime.goal import GoalController, is_obviously_substantial
from server.runtime.scope_guard import build_scope_for_intent, check_tool_in_scope
from server.runtime.task_intent import heuristic_intent
from server.runtime.tool_semantics import resolve_tool_semantics
from session_store import SessionManager

FIXTURE = (
    Path(__file__).resolve().parent / "fixtures" / "accident_dingtalk_one_shot.json"
)


class AccidentDingTalkOneShotReplay(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.case = json.loads(FIXTURE.read_text(encoding="utf-8"))

    def test_fixture_has_no_live_secrets(self):
        blob = FIXTURE.read_text(encoding="utf-8").lower()
        self.assertNotIn("?key=", blob)
        self.assertNotIn("sk-", blob)
        self.assertIn("redacted", blob)

    def test_legacy_keyword_routing_cannot_activate_goal(self):
        req = self.case["user_request"]
        self.assertIsNone(heuristic_intent(req))
        self.assertFalse(is_obviously_substantial(req))

    def test_query_tools_are_query_role(self):
        for name in self.case["expected"]["query_tools_are_non_blocking"]:
            wire = f"mcp__dingtalk_robot_message__{name}"
            sem = resolve_tool_semantics(wire)
            self.assertEqual(sem.goal_role, "query", msg=wire)
            self.assertEqual(sem.effect, "read")

    def test_terminal_send_semantics(self):
        terminal = self.case["expected"]["terminal_tool"]
        wire = f"mcp__dingtalk_robot_message__{terminal}"
        sem = resolve_tool_semantics(wire)
        self.assertEqual(sem.goal_role, "terminal_action")
        self.assertEqual(sem.acceptance_type, "message_sent")

    def test_scope_asks_before_runtime_edit(self):
        from server.runtime.scope_guard import classify_scope

        scope = build_scope_for_intent(
            "one_shot_action",
            user_message=self.case["user_request"],
        )
        gate = classify_scope(
            scope,
            "edit_file",
            {"path": "server/runtime/executor.py", "old_string": "a", "new_string": "b"},
            Path(".").resolve(),
        )
        self.assertIsNotNone(gate)
        assert gate is not None
        self.assertEqual(gate["action"], "ask")
        self.assertEqual(gate["reason"], "scope_runtime_edit")

    def test_goal_controller_does_not_auto_tool_when_disabled(self):
        import tempfile

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        sm = SessionManager(
            Path(tmp.name),
            {"session": {"persist_dir": ".sessions"}},
        )
        state = sm.create_session("assistant_dm", "assistant:test", title="accident")
        controller = GoalController(
            state=state,
            session_manager=sm,
            board_scope="assistant:test",
            run_id="run_accident",
            user_request=self.case["user_request"],
        )
        controller.auto_activate_tools = False
        controller.before_tool(
            "mcp__dingtalk_robot_message__search_groups_by_keyword",
            {"keyword": "x"},
            "tc1",
        )
        self.assertFalse(controller.active)

    def test_chinese_wire_name_still_resolves_semantics(self):
        # Legacy normalized Chinese server collapses to underscores.
        from server.runtime.mcp import normalize_mcp_name

        collapsed = normalize_mcp_name("机器人消息")
        wire = f"mcp__{collapsed}__search_groups_by_keyword"
        sem = resolve_tool_semantics(wire)
        self.assertEqual(sem.goal_role, "query")
        self.assertEqual(sem.server_id, "dingtalk_robot_message")

    def test_executor_prefix_match_for_chinese_server(self):
        from server.runtime.mcp import MCPClient, normalize_mcp_name

        class Stub(MCPClient):
            def call_tool(self, tool_name: str, args: dict) -> str:
                return f"ok:{tool_name}"

        server = "机器人消息"
        executor = ToolExecutor(
            cwd=Path(".").resolve(),
            mcp_clients={server: Stub(server)},
        )
        wire = f"mcp__{normalize_mcp_name(server)}__search_groups_by_keyword"
        resolved = executor._mcp_client_for_tool(wire)
        self.assertIsNotNone(resolved)
        client, tool = resolved  # type: ignore[misc]
        self.assertEqual(tool, "search_groups_by_keyword")
        result = executor._run_mcp(wire, {}, {})
        self.assertEqual(result.outcome, "ok")


if __name__ == "__main__":
    unittest.main()
