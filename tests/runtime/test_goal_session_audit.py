"""System-level Goal / session mutation / run audit invariants (2026-07-24)."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from server.runtime.acceptance import assert_plugin_install_for_texts, plugin_installed
from server.runtime.executor import ToolExecutor
from server.runtime.goal import Evaluation, GoalController
from server.runtime.permissions import classify_permission
from server.runtime.run_transcript import (
    append_run_event,
    read_run_events,
    redact_sensitive_data,
)
from server.runtime.session_actors import ACTOR_USER_UI, assert_destructive_actor
from session_store import SessionManager, SessionMeta, SessionState


class GoalSessionAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        (self.home / "plugins").mkdir()
        (self.home / ".sessions").mkdir()
        (self.home / "runs").mkdir()
        (self.home / "archives").mkdir()
        self._env = patch.dict(os.environ, {"TIGEROSE_HOME": str(self.home)})
        self._env.start()
        self.addCleanup(self._env.stop)
        from avent_paths import reset_path_cache
        from server.db import schema

        reset_path_cache()
        self.addCleanup(reset_path_cache)
        schema.init_db()

    def make_state(self) -> tuple[SessionManager, SessionState]:
        sm = SessionManager(self.home / ".sessions")
        state = sm.create_session(
            "assistant_dm",
            "user:local:assistant:tpl_test",
            title="test",
        )
        return sm, state

    def test_actor_required(self):
        with self.assertRaises(ValueError):
            assert_destructive_actor(None)
        with self.assertRaises(ValueError):
            assert_destructive_actor("agent")
        self.assertEqual(assert_destructive_actor(ACTOR_USER_UI), ACTOR_USER_UI)

    def test_bash_sessions_clear_denied(self):
        gate = classify_permission(
            "bash",
            {"command": "curl -X POST http://127.0.0.1:8765/api/assistants/x/sessions/clear"},
            cwd=self.root,
        )
        self.assertEqual(gate["action"], "deny")
        self.assertEqual(gate["reason"], "session_clear")

    def test_request_clear_session_always_asks(self):
        gate = classify_permission(
            "request_clear_session",
            {"reason": "cleanup"},
            cwd=self.root,
        )
        self.assertEqual(gate["action"], "ask")
        self.assertEqual(gate["reason"], "session_clear")

    def test_trivial_read_does_not_reset_evaluation_blocks(self):
        from server.runtime.executor import ToolResult

        sm, state = self.make_state()
        controller = GoalController(
            state=state,
            session_manager=sm,
            board_scope="assistant:test",
            run_id="run_audit_1",
            user_request="work",
            evaluator=lambda payload: Evaluation(False, "not done", ["more"], "continue"),
        )
        controller.activate("work", source="explicit")
        for i in range(3):
            controller.before_tool("read_file", {"path": f"a{i}.py"}, f"call_{i}")
            controller.after_tool(
                ToolResult(
                    f"content {i}",
                    "ok",
                    {"tool_call_id": f"call_{i}", "tool_name": "read_file"},
                )
            )
            self.assertEqual(controller.on_candidate_stop("done")["action"], "block")
        self.assertEqual(controller.goal["evaluation_blocks"], 3)
        # Fake progress must not wipe evaluation_blocks.
        self.assertGreaterEqual(controller.goal["evaluation_blocks"], 3)

    def test_evaluation_block_hard_ceiling(self):
        sm, state = self.make_state()
        controller = GoalController(
            state=state,
            session_manager=sm,
            board_scope="assistant:test",
            run_id="run_audit_2",
            user_request="work",
            evaluator=lambda payload: Evaluation(False, "missing", ["x"], "do x"),
        )
        controller.activate("work", source="explicit")
        for _ in range(7):
            self.assertEqual(controller.on_candidate_stop("done")["action"], "block")
        self.assertEqual(controller.on_candidate_stop("done")["action"], "suspended")
        self.assertEqual(controller.goal["evaluation_blocks"], 8)

    def test_evaluator_failure_suspends_not_allow(self):
        sm, state = self.make_state()

        def fail(_payload):
            raise TimeoutError("boom")

        controller = GoalController(
            state=state,
            session_manager=sm,
            board_scope="assistant:test",
            run_id="run_audit_3",
            user_request="work",
            evaluator=fail,
        )
        controller.activate("work", source="explicit")
        decision = controller.on_candidate_stop("done")
        self.assertEqual(decision["action"], "suspended")
        self.assertEqual(controller.goal["status"], "suspended")

    def test_plugin_install_acceptance_blocks(self):
        sm, state = self.make_state()
        controller = GoalController(
            state=state,
            session_manager=sm,
            board_scope="assistant:test",
            run_id="run_audit_4",
            user_request="安装 plugins/backend-architect 到系统插件目录",
            evaluator=lambda payload: Evaluation(True, "looks done", [], ""),
        )
        controller.activate(
            "安装 plugins/backend-architect 到系统插件目录",
            source="explicit",
        )
        decision = controller.on_candidate_stop("已完成安装")
        self.assertEqual(decision["action"], "block")
        msg = (decision.get("message") or "").lower()
        self.assertTrue(
            "硬性验收" in decision.get("message", "")
            or "plugin" in msg
            or "acceptance" in msg
        )

    def test_plugin_installed_hard_check(self):
        name = "backend-architect"
        self.assertFalse(plugin_installed(name)["ok"])
        dest = self.home / "plugins" / name
        dest.mkdir()
        (dest / "plugin.yaml").write_text(
            "name: backend-architect\nversion: '1.0.0'\n",
            encoding="utf-8",
        )
        self.assertTrue(plugin_installed(name)["ok"])
        self.assertIsNone(
            assert_plugin_install_for_texts(
                "安装 plugins/backend-architect 插件到系统目录"
            )
        )

    def test_run_transcript_survives(self):
        run_id = "run_transcript_test"
        append_run_event(run_id, {"kind": "trace", "name": "PreToolUse", "detail": "bash"})
        append_run_event(run_id, {"kind": "run_finish", "status": "cancelled"})
        events = read_run_events(run_id)
        self.assertGreaterEqual(len(events), 2)
        self.assertEqual(events[-1]["kind"], "run_finish")

    def test_run_transcript_redacts_credentials_and_preserves_full_result(self):
        result = "result-" + ("x" * 6000)
        append_run_event(
            "run_redacted_audit",
            {
                "kind": "audit",
                "stage": "tool_finished",
                "args": {
                    "url": "https://example.test",
                    "headers": {"Authorization": "Bearer top-secret"},
                    "cookie": "session=private",
                },
                "result": result,
                "metadata": {"api_key": "private-key", "exit_code": 0},
            },
        )
        event = read_run_events("run_redacted_audit")[-1]
        self.assertEqual(event["schema_version"], 1)
        self.assertTrue(event["event_id"].startswith("evt_"))
        self.assertEqual(event["stage"], "tool_finished")
        self.assertEqual(event["result"], result)
        self.assertEqual(event["args"]["headers"]["Authorization"], "[REDACTED]")
        self.assertEqual(event["args"]["cookie"], "[REDACTED]")
        self.assertEqual(event["metadata"]["api_key"], "[REDACTED]")

    def test_recursive_redaction_handles_authorization_values(self):
        redacted = redact_sensitive_data(
            {"detail": "Authorization: Bearer very-secret", "tokens": ["private"]}
        )
        self.assertEqual(redacted["detail"], "Authorization: [REDACTED]")
        self.assertEqual(redacted["tokens"], "[REDACTED]")

    def test_execution_chain_meta_is_kept_compact_for_message_ui(self):
        from server.scheduler.group_scheduler import _execution_meta_from_result

        chain = {"version": 1, "steps": [{"kind": "query", "name": "用户 Query"}]}
        meta = _execution_meta_from_result(
            {
                "capabilities_used": {"skills": ["example"]},
                "capabilities_invoked": {
                    "skills": ["example"],
                    "tools": ["get_skill"],
                },
                "execution_chain": chain,
                "termination": "completed",
                "run_outcome_status": "completed",
                "execution_status": "completed",
                "evidence_status": "verified",
                "result_verdict": "fail",
                "intent": "read_only",
            }
        )
        self.assertEqual(meta["execution_chain"], chain)
        self.assertEqual(meta["capabilities_used"]["skills"], ["example"])
        self.assertEqual(meta["capabilities_invoked"]["tools"], ["get_skill"])
        self.assertEqual(meta["termination"], "completed")
        self.assertEqual(meta["execution_status"], "completed")
        self.assertEqual(meta["evidence_status"], "verified")
        self.assertEqual(meta["result_verdict"], "fail")

    def test_invoked_capabilities_only_counts_actual_calls_and_loaded_skills(self):
        from server.runtime.turn import _invoked_capabilities

        used = _invoked_capabilities(
            [
                {"tool": "read_file", "args": {"path": "README.md"}, "outcome": "ok"},
                {"tool": "read_file", "args": {"path": "AGENTS.md"}, "outcome": "ok"},
                {
                    "tool": "get_skill",
                    "args": {"name": "repo-review"},
                    "result": "# Review",
                    "outcome": "ok",
                },
                {
                    "tool": "get_skill",
                    "args": {"name": "missing"},
                    "result": "Skill not enabled or not found: missing",
                    "outcome": "ok",
                },
                {"tool": "bash", "args": {"command": "pwd"}, "outcome": "denied"},
            ]
        )

        self.assertEqual(used["tools"], ["read_file", "get_skill", "bash"])
        self.assertEqual(used["skills"], ["repo-review"])

    def test_invoked_capabilities_is_empty_without_tool_trace(self):
        from server.runtime.turn import _invoked_capabilities

        self.assertEqual(_invoked_capabilities([]), {"skills": [], "tools": []})

    def test_archive_then_clear(self):
        from server.db import repos
        from server.api import assistant_session_ops as sess
        from server.runtime.session_archive import archives_root

        # Minimal template + messages via repos requires DB init
        from server.db.schema import init_db

        init_db()
        tpl = repos.create_template(
            name="audit-bot",
            role="test",
            system_prompt="x",
        )
        template_id = tpl["template_id"]
        sm, state = self.make_state()
        # Bind scope like assistant DM
        scope = f"user:local:assistant:{template_id}"
        state.meta.scope_key = scope
        sm.save(state)
        sm.bind_scope(scope, state.meta.session_id)

        with patch.object(sess, "get_sessions", return_value=sm), patch.object(
            sess, "scope_for", return_value=scope
        ):
            repos.add_assistant_message(
                template_id,
                "user",
                "hello archive",
                session_id=state.meta.session_id,
            )
            out = sess.clear_current(template_id, actor=ACTOR_USER_UI)
            self.assertNotEqual(out.get("error"), "forbidden")
            # Archive exists
            archive_dirs = list((archives_root() / state.meta.session_id).glob("*"))
            self.assertTrue(archive_dirs)
            msgs_path = archive_dirs[0] / "messages.json"
            data = json.loads(msgs_path.read_text(encoding="utf-8"))
            self.assertTrue(any("hello archive" in str(m.get("content")) for m in data))
            # SQLite cleared
            self.assertEqual(
                repos.count_assistant_messages(template_id, state.meta.session_id),
                0,
            )

    def test_deleted_task_is_pruned_not_pending(self):
        from server.runtime.executor import ToolResult
        from server.runtime.tools import board

        sm, state = self.make_state()
        controller = GoalController(
            state=state,
            session_manager=sm,
            board_scope="assistant:test",
            run_id="run_prune",
            user_request="install plugin",
            evaluator=lambda payload: Evaluation(True, "done", [], ""),
        )
        controller.activate("install plugin", source="explicit")
        task = board.create_task(
            "assistant:test",
            "temp verify",
            goal_id=controller.goal["goal_id"],
            run_id="run_prune",
        )
        controller.after_tool(
            ToolResult(
                f"Created {task['task_id']}",
                "ok",
                {
                    "tool_name": "create_task",
                    "task_transition": {
                        "ok": True,
                        "task_id": task["task_id"],
                        "scope_key": "assistant:test",
                        "old_status": None,
                        "new_status": "pending",
                    },
                },
            )
        )
        self.assertIn(task["task_id"], controller.goal["task_ids"])
        board.delete_task(task["task_id"], expected_scope="assistant:test")
        controller.after_tool(
            ToolResult(
                f"Deleted {task['task_id']}",
                "ok",
                {
                    "tool_name": "delete_task",
                    "task_transition": {
                        "ok": True,
                        "task_id": task["task_id"],
                        "scope_key": "assistant:test",
                        "old_status": "pending",
                        "new_status": "missing",
                    },
                },
            )
        )
        self.assertNotIn(task["task_id"], controller.goal["task_ids"])
        self.assertEqual(controller.pending_items(), [])
        decision = controller.on_candidate_stop("installed")
        # No pending tasks; may still block on plugin acceptance depending on condition.
        self.assertNotIn("missing", decision.get("message", ""))

    def test_stale_missing_binding_pruned_on_pending_check(self):
        sm, state = self.make_state()
        controller = GoalController(
            state=state,
            session_manager=sm,
            board_scope="assistant:test",
            run_id="run_stale",
            user_request="work",
            evaluator=lambda payload: Evaluation(True, "done", [], ""),
        )
        controller.activate("work", source="explicit")
        controller.goal["task_ids"] = ["rtask_ghost_deleted"]
        pending = controller.pending_items()
        self.assertEqual(pending, [])
        self.assertEqual(controller.goal["task_ids"], [])

    def test_acceptance_deny_is_retryable_not_permanently_blocked(self):
        """Acceptance denied complete_task must not enter denied_calls."""
        from server.runtime.executor import ToolResult
        from server.runtime.loop import run_tool_loop

        class Completions:
            def __init__(self):
                self.calls = 0

            def create(self, **kwargs):
                self.calls += 1
                if self.calls <= 2:
                    tool_call = SimpleNamespace(
                        id=f"call_{self.calls}",
                        function=SimpleNamespace(
                            name="complete_task",
                            arguments='{"task_id":"rtask_x"}',
                        ),
                    )
                    message = SimpleNamespace(content="", tool_calls=[tool_call])
                else:
                    message = SimpleNamespace(content="done", tool_calls=[])
                return SimpleNamespace(
                    choices=[
                        SimpleNamespace(message=message, finish_reason="stop")
                    ]
                )

        attempts = {"n": 0}

        def complete_handler(task_id=""):
            attempts["n"] += 1
            if attempts["n"] == 1:
                return ToolResult(
                    "Plugin install acceptance failed",
                    "denied",
                    {
                        "tool_name": "complete_task",
                        "acceptance_ok": False,
                        "recoverable_deny": True,
                        "task_transition": {
                            "ok": False,
                            "task_id": task_id,
                            "scope_key": "assistant:test",
                            "old_status": "in_progress",
                            "new_status": "in_progress",
                        },
                    },
                )
            return ToolResult(
                f"Completed {task_id}",
                "ok",
                {
                    "tool_name": "complete_task",
                    "task_transition": {
                        "ok": True,
                        "task_id": task_id,
                        "scope_key": "assistant:test",
                        "old_status": "in_progress",
                        "new_status": "completed",
                    },
                },
            )

        client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
        executor = ToolExecutor(
            cwd=Path.cwd(),
            dangerous_handlers={"complete_task": complete_handler},
        )
        result = run_tool_loop(
            client=client,
            model="test",
            messages=[{"role": "user", "content": "work"}],
            schemas=[
                {
                    "type": "function",
                    "function": {
                        "name": "complete_task",
                        "parameters": {
                            "type": "object",
                            "properties": {"task_id": {"type": "string"}},
                        },
                    },
                }
            ],
            executor=executor,
            max_tokens=100,
            max_rounds=5,
        )
        self.assertEqual(attempts["n"], 2)
        self.assertEqual(result["termination"], "normal_stop")
        self.assertNotIn("Repeated call blocked", result.get("reply", ""))

    def test_loop_guard_does_not_block_goal_stop(self):
        from server.runtime.executor import ToolResult

        sm, state = self.make_state()
        eval_calls = {"n": 0}

        def evaluator(_payload):
            eval_calls["n"] += 1
            return Evaluation(True, "done", [], "")

        controller = GoalController(
            state=state,
            session_manager=sm,
            board_scope="assistant:test",
            run_id="run_loop_guard",
            user_request="write a note",
            evaluator=evaluator,
        )
        controller.activate("write a note", source="explicit")
        controller.before_tool("read_file", {"path": "a.py"}, "call_lg")
        controller.after_tool(
            ToolResult(
                "Repeated no-progress call blocked after two identical attempts.",
                "error",
                {
                    "tool_call_id": "call_lg",
                    "tool_name": "read_file",
                    "loop_guard": True,
                },
            )
        )
        self.assertEqual(controller.unresolved_tool_failures(), [])
        decision = controller.on_candidate_stop("done")
        self.assertEqual(decision["action"], "allow")
        self.assertEqual(controller.goal["status"], "achieved")
        self.assertEqual(eval_calls["n"], 1)

    def test_plugin_hard_acceptance_short_circuits_evaluator(self):
        from server.runtime.executor import ToolResult

        name = "backend-architect"
        dest = self.home / "plugins" / name
        dest.mkdir()
        (dest / "plugin.yaml").write_text(
            "name: backend-architect\nversion: '1.0.0'\n",
            encoding="utf-8",
        )
        sm, state = self.make_state()
        eval_calls = {"n": 0}

        def never_call(_payload):
            eval_calls["n"] += 1
            return Evaluation(False, "should not run", ["x"], "continue")

        condition = "安装 plugins/backend-architect 到系统插件目录"
        controller = GoalController(
            state=state,
            session_manager=sm,
            board_scope="assistant:test",
            run_id="run_accept_short",
            user_request=condition,
            evaluator=never_call,
        )
        controller.activate(condition, source="explicit")
        # Simulate RepeatedCallBlocked poison that previously blocked stop.
        controller.before_tool("read_file", {"path": "plugin.yaml"}, "call_r")
        controller.after_tool(
            ToolResult(
                "blocked",
                "error",
                {
                    "tool_call_id": "call_r",
                    "tool_name": "read_file",
                    "loop_guard": True,
                },
            )
        )
        snap = controller.acceptance_snapshot("已完成安装")
        self.assertTrue(snap["bindings_closed"])
        self.assertTrue(snap["acceptance_ok"])
        decision = controller.on_candidate_stop("已完成安装")
        self.assertEqual(decision["action"], "allow")
        self.assertEqual(controller.goal["status"], "achieved")
        self.assertEqual(eval_calls["n"], 0)

    def test_gate_theatre_task_not_bound(self):
        from server.runtime.executor import ToolResult
        from server.runtime.goal import should_bind_task_to_goal
        from server.runtime.tools import board

        self.assertFalse(should_bind_task_to_goal("验证门控临时任务"))
        self.assertFalse(should_bind_task_to_goal("clr-gate check"))
        self.assertTrue(should_bind_task_to_goal("安装 backend-architect"))

        sm, state = self.make_state()
        controller = GoalController(
            state=state,
            session_manager=sm,
            board_scope="assistant:test",
            run_id="run_theatre",
            user_request="work",
            evaluator=lambda payload: Evaluation(True, "done", [], ""),
        )
        controller.activate("work", source="explicit")
        task = board.create_task("assistant:test", "验证门控临时任务")
        controller.before_tool(
            "create_task",
            {"subject": "验证门控临时任务"},
            "call_ct",
        )
        controller.after_tool(
            ToolResult(
                f"Created {task['task_id']}",
                "ok",
                {
                    "tool_name": "create_task",
                    "bind_to_goal": False,
                    "task_title": "验证门控临时任务",
                    "task_transition": {
                        "ok": True,
                        "task_id": task["task_id"],
                        "scope_key": "assistant:test",
                        "old_status": None,
                        "new_status": "pending",
                        "title": "验证门控临时任务",
                    },
                },
            )
        )
        self.assertNotIn(task["task_id"], controller.goal["task_ids"])
        self.assertEqual(controller.pending_items(), [])

    def test_readonly_cache_skips_attempt_budget(self):
        from server.runtime.executor import ToolResult
        from server.runtime.loop import run_tool_loop

        class Completions:
            def __init__(self):
                self.calls = 0

            def create(self, **kwargs):
                self.calls += 1
                if self.calls <= 3:
                    tool_call = SimpleNamespace(
                        id=f"call_{self.calls}",
                        function=SimpleNamespace(
                            name="read_file",
                            arguments='{"path":"same.py"}',
                        ),
                    )
                    message = SimpleNamespace(content="", tool_calls=[tool_call])
                else:
                    message = SimpleNamespace(content="done", tool_calls=[])
                return SimpleNamespace(
                    choices=[
                        SimpleNamespace(message=message, finish_reason="stop")
                    ]
                )

        executes = {"n": 0}

        def read_handler(path=""):
            executes["n"] += 1
            return ToolResult(
                f"content of {path}",
                "ok",
                {"tool_name": "read_file", "path": path},
            )

        client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
        executor = ToolExecutor(
            cwd=Path.cwd(),
            dangerous_handlers={"read_file": read_handler},
        )
        result = run_tool_loop(
            client=client,
            model="test",
            messages=[{"role": "user", "content": "read"}],
            schemas=[
                {
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "parameters": {
                            "type": "object",
                            "properties": {"path": {"type": "string"}},
                        },
                    },
                }
            ],
            executor=executor,
            max_tokens=100,
            max_rounds=6,
        )
        # First call executes; 2nd/3rd are cache hits — never RepeatedCallBlocked.
        self.assertEqual(executes["n"], 1)
        self.assertEqual(result["termination"], "normal_stop")
        blocked = [
            t
            for t in result.get("tool_trace") or []
            if "Repeated" in str(t.get("result") or "")
        ]
        self.assertEqual(blocked, [])

    def test_content_heuristic_banned_read_with_error_word_is_ok(self):
        from server.runtime.executor import ToolExecutor
        from server.runtime.tools import files as file_tools

        tmp = self.root / "src"
        tmp.mkdir()
        sample = tmp / "sample.py"
        sample.write_text(
            'def fail():\n    raise RuntimeError("error: expected failure")\n',
            encoding="utf-8",
        )
        executor = ToolExecutor(
            cwd=tmp,
            safe_handlers={
                "read_file": lambda path, limit=None, offset=None: file_tools.read_file(
                    tmp, path, limit=limit, offset=offset
                )
            },
        )
        result = executor.execute("read_file", {"path": "sample.py"})
        self.assertEqual(result.outcome, "ok")
        self.assertIn("error:", result.content)
        self.assertEqual(result.metadata.get("tool_kind"), "query")

    def test_query_failure_never_unresolved(self):
        from server.runtime.executor import ToolResult

        sm, state = self.make_state()
        controller = GoalController(
            state=state,
            session_manager=sm,
            board_scope="assistant:test",
            run_id="run_query_fail",
            user_request="work",
            evaluator=lambda payload: Evaluation(True, "done", [], ""),
        )
        controller.activate("work", source="explicit")
        controller.before_tool("read_file", {"path": "missing.py"}, "c1")
        controller.after_tool(
            ToolResult(
                "File not found: missing.py",
                "error",
                {
                    "tool_call_id": "c1",
                    "tool_name": "read_file",
                    "tool_kind": "query",
                },
            )
        )
        self.assertEqual(controller.unresolved_tool_failures(), [])
        self.assertTrue(any("query" in w for w in controller.runtime_warnings()))
        decision = controller.on_candidate_stop("done")
        self.assertEqual(decision["action"], "allow")

    def test_hard_acceptance_skips_unresolved_mutation_noise(self):
        from server.runtime.executor import ToolResult

        name = "backend-architect"
        dest = self.home / "plugins" / name
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "plugin.yaml").write_text(
            "name: backend-architect\nversion: '1.0.0'\n",
            encoding="utf-8",
        )
        sm, state = self.make_state()
        condition = "安装 plugins/backend-architect 插件"
        controller = GoalController(
            state=state,
            session_manager=sm,
            board_scope="assistant:test",
            run_id="run_heal",
            user_request=condition,
            evaluator=lambda payload: Evaluation(False, "no", ["x"], "x"),
        )
        controller.activate(condition, source="explicit")
        controller.before_tool("write_file", {"path": "x", "content": "y"}, "cw")
        controller.after_tool(
            ToolResult(
                "failed write",
                "error",
                {
                    "tool_call_id": "cw",
                    "tool_name": "write_file",
                    "tool_kind": "mutation",
                },
            )
        )
        self.assertEqual(controller.unresolved_tool_failures(), ["write_file [error]"])
        decision = controller.on_candidate_stop("installed")
        self.assertEqual(decision["action"], "allow")
        self.assertEqual(controller.goal["status"], "achieved")
        self.assertIn("terminal_healed_at", controller.goal)

    def test_bash_query_kind_allowlist(self):
        from server.runtime.tools.registry import bash_is_query, resolve_tool_kind

        self.assertTrue(bash_is_query("ls -la plugins"))
        self.assertTrue(bash_is_query("pwd"))
        self.assertFalse(bash_is_query("ls | rm -rf /"))
        self.assertFalse(bash_is_query("cp a b"))
        self.assertEqual(resolve_tool_kind("bash", {"command": "ls"}), "query")
        self.assertEqual(resolve_tool_kind("bash", {"command": "cp a b"}), "mutation")
