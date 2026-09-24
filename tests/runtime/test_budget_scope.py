from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import sqlite3

from server.runtime.budgets import create_budget, release, reserve
from server.runtime.scope_guard import (
    SCOPE_CAPABILITY_REASON,
    build_scope_for_intent,
    check_tool_in_scope,
    classify_scope,
)


class BudgetScopeTests(unittest.TestCase):
    def test_one_shot_hard_tool_limit_blocks_eighty_first_reserve(self):
        budget = create_budget("one_shot")
        accepted = 0
        for index in range(81):
            reservation_id = f"tool_{index}"
            ok = reserve(
                budget.root_budget_id,
                reservation_id,
                "tool_call",
                1.0,
            )
            if ok:
                accepted += 1
            else:
                release(reservation_id)
        self.assertEqual(accepted, 80)

    def test_one_shot_dingtalk_scope_asks_runtime_edit(self):
        from server.runtime.scope_guard import classify_scope

        scope = build_scope_for_intent(
            "one_shot_action",
            user_message="给钉钉日程测试群发消息",
            enabled_mcp_ids=["mcp:dingtalk_robot_message"],
        )
        with tempfile.TemporaryDirectory() as tmp:
            runtime_path = Path(tmp) / "server" / "runtime" / "foo.py"
            runtime_path.parent.mkdir(parents=True, exist_ok=True)
            runtime_path.write_text("# test", encoding="utf-8")
            gate = classify_scope(
                scope,
                "edit_file",
                {"path": str(runtime_path)},
                cwd=tmp,
            )
        self.assertIsNotNone(gate)
        assert gate is not None
        self.assertEqual(gate["action"], "ask")
        self.assertEqual(gate["reason"], "scope_runtime_edit")
        allowed, _reason = check_tool_in_scope(
            scope,
            "edit_file",
            {"path": "server/runtime/foo.py"},
            cwd=".",
        )
        self.assertTrue(allowed)

    def test_one_shot_scope_still_denies_spawn(self):
        scope = build_scope_for_intent(
            "one_shot_action",
            user_message="给钉钉日程测试群发消息",
            enabled_mcp_ids=["mcp:dingtalk_robot_message"],
        )
        gate = classify_scope(scope, "task", {"prompt": "x"}, cwd=".")
        self.assertIsNotNone(gate)
        assert gate is not None
        self.assertEqual(gate["action"], "deny")

    def test_task_board_and_new_empty_file_do_not_require_scope_permission(self):
        scope = build_scope_for_intent("one_shot_action")
        self.assertIsNone(
            classify_scope(scope, "create_task", {"subject": "Investigate"}, cwd=".")
        )
        self.assertIsNone(
            classify_scope(scope, "claim_task", {"task_id": "task_1"}, cwd=".")
        )

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "server" / "runtime" / "blank.py"
            self.assertIsNone(
                classify_scope(
                    scope,
                    "write_file",
                    {"path": str(target), "content": ""},
                    cwd=tmp,
                )
            )

            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("", encoding="utf-8")
            existing_gate = classify_scope(
                scope,
                "write_file",
                {"path": str(target), "content": ""},
                cwd=tmp,
            )
            self.assertIsNotNone(existing_gate)
            assert existing_gate is not None
            self.assertEqual(existing_gate["reason"], "scope_runtime_edit")

            nonempty_gate = classify_scope(
                scope,
                "write_file",
                {"path": str(Path(tmp) / "server" / "runtime" / "content.py"), "content": "x"},
                cwd=tmp,
            )
            self.assertIsNotNone(nonempty_gate)
            assert nonempty_gate is not None
            self.assertEqual(nonempty_gate["reason"], "scope_runtime_edit")

    def test_managed_connector_cli_does_not_request_scope_approval(self):
        scope = build_scope_for_intent(
            "one_shot_action", connector_executables=["dws"]
        )
        self.assertIsNone(
            classify_scope(scope, "bash", {"command": "dws schema calendar --compact"}, cwd=".")
        )

    def test_verified_readonly_bash_chain_runs_without_scope_approval(self):
        scope = build_scope_for_intent("one_shot_action")
        gate = classify_scope(scope, "bash", {"command": "git status --short && git status"}, cwd=".")
        self.assertIsNone(gate)

    def test_enabled_message_and_calendar_allow_both_writes(self):
        msg = (
            "私聊【黄东生 Aven】提醒他会议快开始了，但是另外两个人还没有接受日程"
        )
        scope = build_scope_for_intent(
            "one_shot_action",
            user_message=msg,
            enabled_mcp_ids=[
                "mcp:dingtalk_robot_message",
                "mcp:dingtalk_calendar",
                "mcp:dingtalk_contacts",
            ],
        )
        self.assertIn("dingtalk.message", scope.allowed_domains)
        self.assertIn("dingtalk.calendar", scope.allowed_domains)
        self.assertIsNone(
            classify_scope(
                scope,
                "mcp__dingtalk_robot_message__send_robot_group_message",
                {"msg": "hi"},
                cwd=".",
            )
        )
        self.assertIsNone(
            classify_scope(
                scope,
                "mcp__dingtalk_calendar__create_calendar_event",
                {"title": "x"},
                cwd=".",
            )
        )

    def test_calendar_only_capability_asks_for_message_write(self):
        scope = build_scope_for_intent(
            "one_shot_action",
            user_message="随便写点什么",
            enabled_mcp_ids=["mcp:dingtalk_calendar"],
        )
        gate = classify_scope(
            scope,
            "mcp__dingtalk_robot_message__send_robot_group_message",
            {"msg": "hi"},
            cwd=".",
        )
        self.assertIsNotNone(gate)
        assert gate is not None
        self.assertEqual(gate["action"], "ask")
        self.assertEqual(gate["reason"], SCOPE_CAPABILITY_REASON)
        self.assertEqual(gate.get("domain"), "dingtalk.message")
        self.assertIn("once", str(gate.get("approval_choices")))

        scope.grant_domain("dingtalk.message")
        self.assertIsNone(
            classify_scope(
                scope,
                "mcp__dingtalk_robot_message__send_robot_group_message",
                {"msg": "hi"},
                cwd=".",
            )
        )

    def test_fix_mcp_keyword_does_not_auto_allow_runtime_edit(self):
        scope = build_scope_for_intent(
            "one_shot_action",
            user_message="帮我修复 mcp 连接的 bug",
            enabled_mcp_ids=["mcp:dingtalk_robot_message"],
        )
        self.assertFalse(scope.allow_runtime_edit)
        gate = classify_scope(
            scope,
            "edit_file",
            {"path": "server/runtime/mcp.py"},
            cwd=".",
        )
        self.assertIsNotNone(gate)
        assert gate is not None
        self.assertEqual(gate["action"], "ask")
        self.assertEqual(gate["reason"], "scope_runtime_edit")
        self.assertIn("once", str(gate.get("approval_choices")))

    def test_budget_survives_memory_clear_via_sqlite(self):
        from server.db import schema
        from server.runtime import budgets as budgets_mod

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = Path(tmp.name) / "budget.db"

        def connect():
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            return conn

        with patch.object(schema, "get_connection", side_effect=connect):
            schema.init_db()
            with patch("server.db.repos.get_connection", side_effect=connect):
                budget = create_budget("one_shot")
                self.assertTrue(
                    reserve(budget.root_budget_id, "pre_reload", "tool_call", 1.0)
                )
                root_id = budget.root_budget_id
                with budgets_mod._store_lock:
                    budgets_mod._budgets.clear()
                    budgets_mod._reservations.clear()
                self.assertTrue(reserve(root_id, "post_reload", "tool_call", 1.0))
                loaded = budgets_mod.load_budget(root_id)
                self.assertIsNotNone(loaded)
                assert loaded is not None
                self.assertGreaterEqual(float(loaded.reserved.get("tool_call", 0)), 1.0)


if __name__ == "__main__":
    unittest.main()
