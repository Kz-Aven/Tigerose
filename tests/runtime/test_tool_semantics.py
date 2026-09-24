from __future__ import annotations

import unittest

from server.runtime.tool_semantics import (
    legacy_tool_kind,
    resolve_tool_semantics,
)


class ToolSemanticsTests(unittest.TestCase):
    def test_mcp_search_is_query(self):
        sem = resolve_tool_semantics(
            "mcp__dingtalk_robot_message__search_groups_by_keyword"
        )
        self.assertEqual(sem.goal_role, "query")
        self.assertEqual(sem.effect, "read")
        self.assertEqual(legacy_tool_kind(sem), "query")

    def test_send_robot_group_message_is_terminal_action(self):
        sem = resolve_tool_semantics(
            "send_robot_group_message",
            server_id="dingtalk_robot_message",
        )
        self.assertEqual(sem.goal_role, "terminal_action")
        self.assertEqual(sem.effect, "write")
        self.assertEqual(sem.acceptance_type, "message_sent")
        self.assertEqual(legacy_tool_kind(sem), "mutation")

    def test_chinese_server_alias_resolves(self):
        sem = resolve_tool_semantics(
            "mcp__机器人消息__search_groups_by_keyword",
        )
        self.assertEqual(sem.server_id, "dingtalk_robot_message")
        self.assertEqual(sem.goal_role, "query")
        self.assertEqual(sem.domain, "dingtalk.group.query")

    def test_unknown_mcp_is_fail_safe(self):
        sem = resolve_tool_semantics("mcp__unknown_server__do_something")
        self.assertEqual(sem.effect, "unknown")
        self.assertIn(sem.goal_role, {"unknown", "supporting_mutation"})
        self.assertEqual(legacy_tool_kind(sem), "mutation")


if __name__ == "__main__":
    unittest.main()
