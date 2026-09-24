from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from session_store import SessionManager, SessionMeta, SessionState
from server.runtime import permissions
from server.runtime.context import TurnContext
from server.runtime.executor import ToolExecutor, ToolResult
from server.runtime.goal import (
    Evaluation,
    GoalController,
    parse_evaluation_json,
    parse_goal_command,
)
from server.runtime.loop import run_tool_loop
from server.runtime.run_coordinator import (
    RunBusyError,
    RunCancelledError,
    SessionRunCoordinator,
)
from server.runtime.tools.delegate import run_subagent
from server.api import assistant_session_ops


class GoalRuntimeTests(unittest.TestCase):
    def make_state(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        sm = SessionManager(
            Path(tmp.name),
            {"session": {"persist_dir": ".sessions"}},
        )
        state = sm.create_session("assistant_dm", "assistant:test", title="test")
        return sm, state

    def test_goal_command_parser(self):
        self.assertEqual(parse_goal_command("/goal").action, "status")
        self.assertEqual(parse_goal_command("/goal clear").action, "clear")
        self.assertEqual(parse_goal_command("/goal resume").action, "resume")
        self.assertEqual(parse_goal_command("/goal 继续").action, "resume")
        self.assertEqual(parse_goal_command("@Agent /goal ship it").condition, "ship it")
        self.assertIsNone(parse_goal_command("explain /goals"))

    def test_canonical_state_persists_goal_and_version(self):
        sm, state = self.make_state()
        controller = GoalController(
            state=state,
            session_manager=sm,
            board_scope="assistant:test",
            run_id="run_1",
            user_request="implement it",
        )
        controller.activate("implement it", source="explicit")
        loaded = sm.load(state.meta.session_id)
        self.assertGreater(loaded.version, 0)
        self.assertEqual(loaded.context["goal"]["condition"], "implement it")
        self.assertTrue(
            (sm.root / state.meta.session_id / "state.json").is_file()
        )

    def test_pending_todo_blocks_then_evaluator_achieves(self):
        sm, state = self.make_state()
        evaluations = []

        def evaluator(payload):
            evaluations.append(payload)
            return Evaluation(True, "evidence sufficient", [], "", "eval")

        controller = GoalController(
            state=state,
            session_manager=sm,
            board_scope="assistant:test",
            run_id="run_1",
            user_request="implement it",
            evaluator=evaluator,
        )
        controller.before_tool("todo_write", {}, "call_1")
        state.todos = [{"id": "new", "content": "work", "status": "pending"}]
        controller.after_tool(
            ToolResult(
                "updated",
                "ok",
                {
                    "tool_call_id": "call_1",
                    "tool_name": "todo_write",
                    "todo_ids": ["new"],
                },
            )
        )
        decision = controller.on_candidate_stop("done")
        self.assertEqual(decision["action"], "block")
        self.assertEqual(controller.goal["blocked_rounds"], 0)
        self.assertFalse(evaluations)

        state.todos[0]["status"] = "completed"
        decision = controller.on_candidate_stop("done")
        self.assertEqual(decision["action"], "allow")
        self.assertEqual(controller.goal["status"], "achieved")
        self.assertEqual(len(evaluations), 1)

    def test_mcp_timeout_auto_activates_and_blocks_stop(self):
        sm, state = self.make_state()
        controller = GoalController(
            state=state,
            session_manager=sm,
            board_scope="assistant:test",
            run_id="run_1",
            user_request="analyze the codebase and finish the report",
            evaluator=lambda payload: Evaluation(True, "done", [], "", "eval"),
        )
        controller.before_tool("mcp__codegraph__explore", {"query": "goal"}, "call_1")
        controller.after_tool(
            ToolResult(
                "Tool worker timeout after 180.0s",
                "timeout",
                {
                    "tool_call_id": "call_1",
                    "tool_name": "mcp__codegraph__explore",
                },
            )
        )
        self.assertEqual(controller.goal["source"], "auto_tool")
        decision = controller.on_candidate_stop("done")
        self.assertEqual(decision["action"], "block")
        self.assertTrue(
            "失败" in decision["message"] or "timeout" in decision["message"].lower()
        )

    def test_goal_save_uses_version_cas(self):
        sm, state = self.make_state()
        stale = sm.load(state.meta.session_id)
        controller = GoalController(
            state=state,
            session_manager=sm,
            board_scope="assistant:test",
            run_id="run_1",
            user_request="implement it",
        )
        controller.activate("implement it", source="explicit")
        stale.context["other"] = True
        with self.assertRaises(RuntimeError):
            sm.save(stale, expected_version=stale.version)

    def test_authoritative_save_retries_version_conflict(self):
        sm, state = self.make_state()
        held = sm.load(state.meta.session_id)
        # Simulate a racing meta-only writer advancing the disk version.
        raced = sm.load(state.meta.session_id)
        raced.meta.title = "raced title"
        sm.save(raced, expected_version=raced.version)
        held.messages.append({"role": "user", "content": "keep me"})
        new_version = sm.save(held, expected_version=held.version, retries=3)
        reloaded = sm.load(state.meta.session_id)
        self.assertEqual(new_version, reloaded.version)
        self.assertEqual(reloaded.messages[-1]["content"], "keep me")

    def test_session_refresh_does_not_write_during_run(self):
        sm, state = self.make_state()
        before = state.version
        with patch.object(
            assistant_session_ops,
            "get_sessions",
            return_value=sm,
        ), patch(
            "server.runtime.run_coordinator.coordinator.is_busy",
            return_value=True,
        ):
            assistant_session_ops._sync_one(
                sm,
                "template-test",
                state.meta.session_id,
            )
        self.assertEqual(sm.load(state.meta.session_id).version, before)

    def test_unchanged_meta_sync_is_noop(self):
        sm, state = self.make_state()
        before = state.version
        sm.sync_meta_counts(
            state.meta.session_id,
            message_count=state.meta.message_count,
            last_active_at=state.meta.last_active_at,
            title=state.meta.title,
        )
        self.assertEqual(sm.load(state.meta.session_id).version, before)

    def test_eighth_no_progress_block_suspends(self):
        sm, state = self.make_state()
        controller = GoalController(
            state=state,
            session_manager=sm,
            board_scope="assistant:test",
            run_id="run_1",
            user_request="work",
            evaluator=lambda payload: Evaluation(False, "missing test", ["test"], "run it"),
        )
        controller.activate("work", source="explicit")
        for _ in range(7):
            self.assertEqual(controller.on_candidate_stop("done")["action"], "block")
        self.assertEqual(controller.on_candidate_stop("done")["action"], "suspended")
        self.assertEqual(controller.goal["blocked_rounds"], 8)
        self.assertEqual(controller.goal["status"], "suspended")

    def test_new_progress_does_not_consume_no_progress_budget(self):
        sm, state = self.make_state()
        controller = GoalController(
            state=state,
            session_manager=sm,
            board_scope="assistant:test",
            run_id="run_1",
            user_request="work",
            evaluator=lambda payload: Evaluation(False, "not done", ["more"], "continue"),
        )
        controller.activate("work", source="explicit")
        # Meaningful progress: task status transition
        controller.before_tool("complete_task", {"task_id": "t1"}, "call_1")
        controller.after_tool(
            ToolResult(
                "completed t1",
                "ok",
                {
                    "tool_call_id": "call_1",
                    "tool_name": "complete_task",
                    "task_transition": {
                        "ok": True,
                        "task_id": "t1",
                        "scope_key": "assistant:test",
                        "old_status": "in_progress",
                        "new_status": "completed",
                    },
                },
            )
        )
        self.assertEqual(controller.on_candidate_stop("done")["action"], "block")
        # Meaningful progress resets blocked_rounds for this block cycle.
        self.assertEqual(controller.goal["blocked_rounds"], 0)
        # But evaluation_blocks still increments (hard ceiling).
        self.assertEqual(controller.goal["evaluation_blocks"], 1)
        self.assertEqual(controller.on_candidate_stop("done")["action"], "block")
        self.assertEqual(controller.goal["blocked_rounds"], 1)
        self.assertEqual(controller.goal["evaluation_blocks"], 2)

    def test_trivial_read_is_not_meaningful_progress(self):
        sm, state = self.make_state()
        controller = GoalController(
            state=state,
            session_manager=sm,
            board_scope="assistant:test",
            run_id="run_1",
            user_request="work",
            evaluator=lambda payload: Evaluation(False, "not done", ["more"], "continue"),
        )
        controller.activate("work", source="explicit")
        controller.before_tool("read_file", {"path": "a.py"}, "call_1")
        controller.after_tool(
            ToolResult(
                "new content",
                "ok",
                {"tool_call_id": "call_1", "tool_name": "read_file"},
            )
        )
        self.assertEqual(controller.on_candidate_stop("done")["action"], "block")
        self.assertEqual(controller.goal["blocked_rounds"], 1)
        self.assertEqual(controller.goal["evaluation_blocks"], 1)

    def test_subagent_uses_configured_max_iterations(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        ctx = TurnContext(
            template_id="tpl",
            scope_key="assistant:tpl",
            session_id="sess_test",
            cwd=Path(tmp.name),
            surface="assistant_dm",
        )
        fake_loop_result = {
            "reply": "[tool loop reached max rounds]",
            "termination": "max_turns",
            "rounds": 50,
        }
        with patch(
            "server.runtime.loop.run_tool_loop",
            return_value=fake_loop_result,
        ) as mocked_loop, patch(
            "avent_config.get_config",
            return_value={
                "delegation": {"max_iterations": 50},
                "agent": {"tool_timeout_s": 180},
            },
        ):
            result = run_subagent(
                ctx=ctx,
                client=SimpleNamespace(),
                model="test",
                prompt="inspect",
            )
        self.assertEqual(mocked_loop.call_args.kwargs["max_rounds"], 50)
        self.assertEqual(result.outcome, "error")
        self.assertEqual(result.metadata["subagent_status"], "max_rounds")

    @patch("server.runtime.goal.board.fail_task")
    @patch("server.runtime.goal.board.get_task")
    def test_subagent_failure_fails_bound_claimed_task(
        self,
        get_task,
        fail_task,
    ):
        sm, state = self.make_state()
        controller = GoalController(
            state=state,
            session_manager=sm,
            board_scope="assistant:test",
            run_id="run_1",
            user_request="work",
        )
        controller.activate("work", source="explicit")
        controller.goal["task_ids"] = ["task_1"]
        get_task.return_value = {
            "task_id": "task_1",
            "scope_key": "assistant:test",
            "status": "in_progress",
        }
        controller.before_tool("task", {"prompt": "work"}, "call_1")
        controller.after_tool(
            ToolResult(
                "Sub-agent failed (max_rounds)",
                "error",
                {
                    "tool_call_id": "call_1",
                    "tool_name": "task",
                    "subagent_status": "max_rounds",
                },
            )
        )
        fail_task.assert_called_once_with(
            "task_1",
            "sub-agent max_rounds",
            expected_scope="assistant:test",
        )

    def test_bash_external_path_uses_file_permission_gate(self):
        tmp = tempfile.TemporaryDirectory()
        other = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.addCleanup(other.cleanup)
        policy = SimpleNamespace(
            monitors_dangerous_bash=lambda: False,
            file_access="workspace",
            danger_policy="deny",
        )
        gate = permissions.classify_permission(
            "bash",
            {"command": f'cat "{Path(other.name) / "secret.txt"}"'},
            cwd=Path(tmp.name),
            policy=policy,
        )
        self.assertEqual(gate["reason"], "external_path")
        self.assertEqual(gate["action"], "deny")

    def test_evaluator_retries_then_suspends_without_mutation_evidence(self):
        sm, state = self.make_state()
        calls = []

        def fail(payload):
            calls.append(payload)
            raise TimeoutError("evaluator timeout")

        controller = GoalController(
            state=state,
            session_manager=sm,
            board_scope="assistant:test",
            run_id="run_1",
            user_request="work",
            evaluator=fail,
        )
        controller.activate("work", source="explicit")
        decision = controller.on_candidate_stop("done")
        self.assertEqual(decision["action"], "suspended")
        self.assertEqual(len(calls), 2)
        self.assertEqual(controller.goal["status"], "suspended")
        self.assertIn("暂时失败", decision["message"])

    def test_evaluator_parse_failure_suspends_without_domain_acceptance(self):
        sm, state = self.make_state()

        def fail(payload):
            raise ValueError("evaluator returned no JSON object")

        controller = GoalController(
            state=state,
            session_manager=sm,
            board_scope="assistant:test",
            run_id="run_1",
            user_request="create calendar",
            evaluator=fail,
        )
        controller.activate("create calendar", source="explicit")
        controller.goal["evidence"] = [
            {
                "name": "mcp__dingtalk_calendar__create_calendar_event",
                "outcome": "ok",
                "tool_kind": "mutation",
                "summary": '{"result":{"iCalUID":"abc","status":"confirmed"}}',
                "loop_guard": False,
                "permission_denied": False,
            }
        ]
        decision = controller.on_candidate_stop("日程已创建")
        self.assertEqual(decision["action"], "suspended")
        self.assertEqual(controller.goal["status"], "suspended")

    @patch("server.runtime.feature_flags.flag_enabled", return_value=False)
    def test_evaluator_parse_failure_achieves_with_mcp_evidence_when_domain_ff_off(
        self, _flag
    ):
        sm, state = self.make_state()

        def fail(payload):
            raise ValueError("evaluator returned no JSON object")

        controller = GoalController(
            state=state,
            session_manager=sm,
            board_scope="assistant:test",
            run_id="run_1",
            user_request="create calendar",
            evaluator=fail,
        )
        controller.activate("create calendar", source="explicit")
        controller.goal["evidence"] = [
            {
                "name": "mcp__dingtalk_calendar__create_calendar_event",
                "outcome": "ok",
                "tool_kind": "mutation",
                "summary": '{"result":{"iCalUID":"abc","status":"confirmed"}}',
                "loop_guard": False,
                "permission_denied": False,
            }
        ]
        decision = controller.on_candidate_stop("日程已创建")
        self.assertEqual(decision["action"], "allow")
        self.assertEqual(controller.goal["status"], "achieved")

    def test_evaluator_parse_failure_achieves_with_acceptance_receipt(self):
        sm, state = self.make_state()

        def fail(payload):
            raise ValueError("evaluator returned no JSON object")

        controller = GoalController(
            state=state,
            session_manager=sm,
            board_scope="assistant:test",
            run_id="run_1",
            user_request="create calendar",
            evaluator=fail,
        )
        controller.activate("create calendar", source="explicit")
        controller.goal["evidence"] = [
            {
                "name": "mcp__dingtalk_calendar__create_calendar_event",
                "outcome": "ok",
                "tool_kind": "mutation",
                "summary": '{"result":{"iCalUID":"abc","status":"confirmed"}}',
                "loop_guard": False,
                "permission_denied": False,
                "acceptance_receipt": {"receipt_id": "ical_abc"},
            }
        ]
        decision = controller.on_candidate_stop("日程已创建")
        self.assertEqual(decision["action"], "allow")
        self.assertEqual(controller.goal["status"], "achieved")

    def test_evaluation_json_extracts_first_object(self):
        result = parse_evaluation_json(
            'prefix {"completed": false, "reason": "missing", '
            '"missing_evidence": ["test"], "next_action": "run tests"} trailing'
        )
        self.assertFalse(result.completed)
        self.assertEqual(result.next_action, "run tests")

    def test_evaluation_json_reads_markdown_fence_and_reasoning_helper(self):
        from server.runtime.goal import extract_assistant_text

        fenced = parse_evaluation_json(
            '```json\n{"completed": true, "reason": "done", '
            '"missing_evidence": [], "next_action": ""}\n```'
        )
        self.assertTrue(fenced.completed)
        msg = SimpleNamespace(content="", reasoning_content='{"completed": false, "reason": "x", "missing_evidence": [], "next_action": "y"}')
        raw = extract_assistant_text(msg)
        parsed = parse_evaluation_json(raw)
        self.assertFalse(parsed.completed)
        self.assertEqual(parsed.next_action, "y")

    def test_executor_default_and_timeout_outcome(self):
        executor = ToolExecutor(
            cwd=Path.cwd(),
            dangerous_handlers={"slow": lambda: time.sleep(0.05)},
            timeout_s=0.01,
        )
        result = executor.execute("slow", {}, tool_call_id="c1")
        self.assertEqual(result.outcome, "timeout")
        self.assertTrue(result.metadata["quarantined"])
        self.assertEqual(
            ToolExecutor(cwd=Path.cwd()).timeout_s,
            180.0,
        )

    def test_no_tool_loop_uses_stop_gate(self):
        class Completions:
            def __init__(self):
                self.calls = 0

            def create(self, **kwargs):
                self.calls += 1
                return SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(content=f"answer {self.calls}", tool_calls=[]),
                            finish_reason="stop",
                        )
                    ]
                )

        completions = Completions()
        client = SimpleNamespace(chat=SimpleNamespace(completions=completions))

        class Gate:
            run_id = "run_1"

            def __init__(self):
                self.calls = 0

            def on_candidate_stop(self, text):
                self.calls += 1
                if self.calls == 1:
                    return {"action": "block", "message": "continue"}
                return {"action": "allow"}

            def suspend_for_limit(self):
                raise AssertionError("should not hit limit")

        gate = Gate()
        result = run_tool_loop(
            client=client,
            model="test",
            messages=[{"role": "user", "content": "work"}],
            schemas=[],
            executor=ToolExecutor(cwd=Path.cwd()),
            max_tokens=100,
            max_rounds=3,
            goal_controller=gate,
        )
        self.assertEqual(result["termination"], "normal_stop")
        self.assertEqual(result["reply"], "answer 2")
        self.assertEqual(gate.calls, 2)

    def test_intermediate_content_splits_thinking_trace(self):
        class Completions:
            def __init__(self):
                self.calls = 0

            def create(self, **kwargs):
                self.calls += 1
                if self.calls < 3:
                    tool_call = SimpleNamespace(
                        id=f"call_{self.calls}",
                        function=SimpleNamespace(
                            name="read_file",
                            arguments='{"path":"sample.txt"}',
                        ),
                    )
                    content = (
                        "我先检查文件。"
                        if self.calls == 1
                        else "文件已检查，继续处理。"
                    )
                    message = SimpleNamespace(content=content, tool_calls=[tool_call])
                else:
                    message = SimpleNamespace(content="处理完成。", tool_calls=[])
                return SimpleNamespace(
                    choices=[SimpleNamespace(message=message, finish_reason="stop")]
                )

        intermediate: list[tuple[str, list[dict]]] = []
        client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
        executor = ToolExecutor(
            cwd=Path.cwd(),
            safe_handlers={"read_file": lambda path: f"contents of {path}"},
        )
        result = run_tool_loop(
            client=client,
            model="test",
            messages=[{"role": "user", "content": "work"}],
            schemas=[
                {
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "parameters": {"type": "object"},
                    },
                }
            ],
            executor=executor,
            max_tokens=100,
            max_rounds=4,
            on_intermediate_content=lambda content, steps: intermediate.append(
                (content, steps)
            ),
        )
        self.assertEqual(
            [content for content, _ in intermediate],
            ["我先检查文件。", "文件已检查，继续处理。"],
        )
        self.assertEqual(intermediate[0][1], [])
        self.assertTrue(
            any(step["kind"] == "tool_result" for step in intermediate[1][1])
        )
        displayed = result["display_thinking_steps"]
        self.assertTrue(any(step["kind"] == "tool_result" for step in displayed))
        self.assertLess(len(displayed), len(result["thinking_steps"]))

    def test_third_identical_tool_call_is_blocked(self):
        """Non-readonly identical calls still hit RunLoopGuard after 2 attempts."""

        class Completions:
            def __init__(self):
                self.calls = 0

            def create(self, **kwargs):
                self.calls += 1
                if self.calls <= 3:
                    tool_call = SimpleNamespace(
                        id=f"call_{self.calls}",
                        function=SimpleNamespace(
                            name="bash",
                            arguments='{"command":"touch same.txt"}',
                        ),
                    )
                    message = SimpleNamespace(content="", tool_calls=[tool_call])
                else:
                    message = SimpleNamespace(content="done", tool_calls=[])
                return SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=message,
                            finish_reason="stop",
                        )
                    ]
                )

        client = SimpleNamespace(
            chat=SimpleNamespace(completions=Completions())
        )
        executor = ToolExecutor(
            cwd=Path.cwd(),
            dangerous_handlers={"bash": lambda command: "same content"},
        )
        result = run_tool_loop(
            client=client,
            model="test",
            messages=[{"role": "user", "content": "work"}],
            schemas=[
                {
                    "type": "function",
                    "function": {
                        "name": "bash",
                        "parameters": {"type": "object"},
                    },
                }
            ],
            executor=executor,
            max_tokens=100,
            max_rounds=5,
        )
        self.assertIn(
            "Repeated no-progress call blocked",
            result["tool_trace"][2]["result"],
        )
        self.assertTrue(
            (result["tool_trace"][2].get("metadata") or {}).get("loop_guard")
        )

    @patch("server.runtime.run_coordinator.repos")
    def test_begin_clears_active_on_upsert_failure(self, mock_repos):
        coordinator = SessionRunCoordinator()
        mock_repos.upsert_runtime_run.side_effect = RuntimeError("db down")
        run_id = coordinator.enqueue("sess_1", agent_id="a")
        with self.assertRaises(RuntimeError):
            coordinator.begin("sess_1", run_id=run_id)
        self.assertFalse(coordinator.is_busy("sess_1"))
        run_id2 = coordinator.enqueue("sess_1", agent_id="a")
        self.assertTrue(run_id2)

    @patch("server.runtime.run_coordinator.repos")
    def test_coordinator_rejects_same_session_concurrency(self, mock_repos):
        coordinator = SessionRunCoordinator()
        run_id = coordinator.enqueue("sess_1", agent_id="a")
        token = coordinator.begin("sess_1", run_id=run_id)
        with self.assertRaises(RunBusyError):
            coordinator.enqueue("sess_1", agent_id="a")
        coordinator.finish(token, "completed")
        mock_repos.finish_runtime_run.assert_called()

    @patch("server.runtime.run_coordinator.repos")
    def test_cancel_queued_run_clears_busy_so_session_can_enqueue_again(self, mock_repos):
        """Rollback after enqueue must not permanently lock the session.

        Reproduces: /goal resume with no suspended goal, or group multi-enqueue
        rollback — cancel() used to leave run_id in _queued forever.
        """
        coordinator = SessionRunCoordinator()
        run_id = coordinator.enqueue("sess_1", agent_id="a")
        self.assertTrue(coordinator.is_busy("sess_1"))
        self.assertTrue(coordinator.cancel(run_id))
        self.assertFalse(coordinator.is_busy("sess_1"))
        run_id2 = coordinator.enqueue("sess_1", agent_id="a")
        self.assertNotEqual(run_id, run_id2)
        mock_repos.finish_runtime_run.assert_called_with(run_id, status="cancelled")

    @patch("server.runtime.run_coordinator.repos")
    def test_cancel_session_clears_queued_runs(self, mock_repos):
        coordinator = SessionRunCoordinator()
        run_id = coordinator.enqueue("sess_1", agent_id="a")
        coordinator.cancel_session("sess_1")
        self.assertFalse(coordinator.is_busy("sess_1"))
        mock_repos.finish_runtime_run.assert_called_with(run_id, status="cancelled")
        run_id2 = coordinator.enqueue("sess_1", agent_id="a")
        self.assertTrue(run_id2)

    @patch("server.runtime.run_coordinator.repos")
    def test_cancelled_queued_run_is_fenced_when_worker_starts(self, mock_repos):
        coordinator = SessionRunCoordinator()
        run_id = coordinator.enqueue("sess_1", agent_id="a")
        self.assertTrue(coordinator.cancel(run_id))
        # Late worker still holding the cancelled run_id must be fenced.
        token = coordinator.begin("sess_1", run_id=run_id)
        self.assertEqual(token.run_id, run_id)
        with self.assertRaises(RunCancelledError):
            token.checkpoint()
        coordinator.finish(token, "cancelled")

    @patch("server.runtime.run_coordinator.repos")
    def test_group_partial_enqueue_rollback_does_not_lock_first_member(self, mock_repos):
        """Mirrors groups.py: enqueue A, enqueue B fails, cancel A."""
        coordinator = SessionRunCoordinator()
        run_a = coordinator.enqueue("sess_a", agent_id="a")
        run_b = coordinator.enqueue("sess_b", agent_id="b")
        token_b = coordinator.begin("sess_b", run_id=run_b)
        with self.assertRaises(RunBusyError):
            coordinator.enqueue("sess_b", agent_id="b2")
        # Rollback previously enqueued A (as groups.py does on busy).
        coordinator.cancel(run_a)
        self.assertFalse(coordinator.is_busy("sess_a"))
        coordinator.finish(token_b, "completed")
        # A can accept work again without restarting the process.
        coordinator.enqueue("sess_a", agent_id="a")


if __name__ == "__main__":
    unittest.main()
