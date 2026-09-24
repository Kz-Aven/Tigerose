from __future__ import annotations

import asyncio
import copy
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from session_store import SessionManager
from server.api.questions import AnswerBody, QuestionAnswer, answer_question
from server.db import repos, schema
from server.runtime.executor import ToolExecutor, ToolResult
from server.runtime.goal import GoalController
from server.runtime.loop import run_tool_loop
from server.runtime.tools.questions import ask_user_question, validate_questions


QUESTIONS = [
    {
        "id": "target",
        "header": "目标",
        "question": "请选择目标环境",
        "options": [
            {"label": "测试", "description": "测试环境"},
            {"label": "生产", "description": "生产环境"},
        ],
        "multi_select": False,
    }
]


class AskUserQuestionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.enterContext(patch.dict(os.environ, {"TIGEROSE_HOME": tmp.name}))
        self.enterContext(patch("avent_paths._DATA_ROOT", Path(tmp.name)))
        schema.init_db()

    def make_db(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = Path(tmp.name) / "test.db"

        def connect():
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            return conn

        with patch.object(schema, "get_connection", side_effect=connect):
            schema.init_db()
        return connect

    def make_state(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        sm = SessionManager(
            Path(tmp.name),
            {"session": {"persist_dir": ".sessions"}},
        )
        return sm, sm.create_session("assistant_dm", "assistant:test")

    def test_schema_validation_and_other_text_contract(self):
        normalized = validate_questions(QUESTIONS)
        self.assertEqual(normalized[0]["id"], "target")
        body = AnswerBody(
            answers=[
                QuestionAnswer(
                    question_id="target",
                    selected_labels=[],
                    other_text="预发布环境",
                )
            ]
        )
        self.assertEqual(body.answers[0].other_text, "预发布环境")
        with self.assertRaisesRegex(ValueError, "1-4"):
            validate_questions([])
        with self.assertRaisesRegex(ValueError, "2-4"):
            validate_questions([{**QUESTIONS[0], "options": QUESTIONS[0]["options"][:1]}])

    def test_executor_ignores_ask_user_question_schema_noise(self):
        received: dict = {}

        def ask_handler(questions, _tool_call_id=""):
            received["questions"] = questions
            received["tool_call_id"] = _tool_call_id
            return ToolResult("waiting", "waiting")

        executor = ToolExecutor(
            cwd=Path.cwd(),
            safe_handlers={"ask_user_question": ask_handler},
        )
        result = executor.execute(
            "ask_user_question",
            {
                "minItems": 1,
                "max_items": 1,
                "questions": QUESTIONS,
            },
            tool_call_id="call_ask_1",
        )
        self.assertEqual(result.outcome, "waiting")
        self.assertEqual(received["questions"], QUESTIONS)
        self.assertEqual(received["tool_call_id"], "call_ask_1")
        self.assertEqual(
            result.metadata["ignored_schema_arguments"],
            ["max_items", "minItems"],
        )

    def test_sqlite_is_authoritative_across_connections(self):
        connect = self.make_db()
        with patch.object(repos, "get_connection", side_effect=connect):
            item = repos.create_pending_question(
                tool_call_id="call_1",
                goal_id="goal_1",
                run_id="run_1",
                session_id="sess_1",
                channel="assistant:tpl",
                surface="assistant_dm",
                template_id="tpl",
                group_id="",
                instance_id="",
                payload={"questions": QUESTIONS},
            )
            loaded = repos.list_pending_questions("assistant:tpl")
            self.assertEqual([row["question_id"] for row in loaded], [item["question_id"]])
            self.assertEqual(loaded[0]["session"], "sess_1")
            self.assertEqual(loaded[0]["questions"][0]["id"], "target")
            self.assertEqual(item["questions"][0]["header"], "目标")
            answered = repos.answer_pending_question(
                item["question_id"],
                [
                    {
                        "question_id": "target",
                        "selected_labels": ["测试"],
                        "other_text": "",
                    }
                ],
            )
            self.assertEqual(answered["status"], "answered")
            self.assertEqual(repos.list_pending_questions("assistant:tpl"), [])

    def test_question_payload_keeps_prior_usage_run_ids(self):
        ctx = SimpleNamespace(
            session_state=SimpleNamespace(
                context={
                    "pending_question_continuation": {
                        "usage_run_ids": ["run_question", "run_question"],
                    }
                }
            ),
            run_id="run_resume",
            session_id="session_1",
            sse_channel="assistant:tpl",
            surface="assistant_dm",
            template_id="tpl",
            group_id="",
            instance_id="",
        )
        with patch.object(repos, "create_pending_question", return_value={"question_id": "pq_1"}) as create:
            ask_user_question(ctx, QUESTIONS, tool_call_id="call_1")

        self.assertEqual(
            create.call_args.kwargs["payload"]["usage_run_ids"],
            ["run_question", "run_resume"],
        )

    def test_transcript_helpers_format_content(self):
        from server.runtime import ask_user_transcript as ask_tx

        pending = {
            "question_id": "pq_1",
            "questions": QUESTIONS,
        }
        self.assertIn("目标", ask_tx.assistant_content(pending))
        text = ask_tx.answer_content(
            pending,
            [{"question_id": "target", "selected_labels": ["测试"], "other_text": ""}],
        )
        self.assertEqual(text, "目标：测试")
        meta = ask_tx.build_ask_user_meta(pending, status="pending")
        self.assertEqual(meta["question_id"], "pq_1")
        self.assertEqual(meta["status"], "pending")
        self.assertEqual(len(meta["questions"]), 1)

    def test_tool_loop_suspends_without_stop_gate_or_timeout(self):
        tool_call = SimpleNamespace(
            id="call_1",
            function=SimpleNamespace(
                name="ask_user_question",
                arguments='{"questions": []}',
            ),
        )
        response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="", tool_calls=[tool_call]),
                    finish_reason="tool_calls",
                )
            ]
        )
        client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=lambda **kwargs: response)
            )
        )

        class Gate:
            run_id = "run_1"

            def __init__(self):
                self.marked = ""

            def before_tool(self, *args):
                pass

            def after_tool(self, result):
                self.assert_waiting = result.outcome

            def mark_waiting_for_user(self, question_id):
                self.marked = question_id

            def on_candidate_stop(self, text):
                raise AssertionError("Stop Gate must not evaluate a waiting turn")

        gate = Gate()
        executor = ToolExecutor(
            cwd=Path.cwd(),
            safe_handlers={
                "ask_user_question": lambda questions, _tool_call_id="": ToolResult(
                    "waiting",
                    "waiting",
                    {
                        "question_id": "pq_1",
                        "pending_question": {
                            "question_id": "pq_1",
                            "tool_call_id": _tool_call_id,
                        },
                    },
                )
            },
            timeout_s=0.001,
        )
        result = run_tool_loop(
            client=client,
            model="test",
            messages=[{"role": "user", "content": "need input"}],
            schemas=[],
            executor=executor,
            max_tokens=100,
            max_rounds=1,
            goal_controller=gate,
        )
        self.assertEqual(result["termination"], "waiting_for_user")
        self.assertEqual(gate.marked, "pq_1")
        self.assertEqual(result["api_messages"][-1]["role"], "assistant")
        self.assertEqual(
            result["api_messages"][-1]["tool_calls"][0]["id"],
            "call_1",
        )
        self.assertFalse(any(m.get("role") == "tool" for m in result["api_messages"]))

    def test_waiting_goal_does_not_increment_no_progress_count(self):
        sm, state = self.make_state()
        controller = GoalController(
            state=state,
            session_manager=sm,
            board_scope="assistant:test",
            run_id="run_1",
            user_request="work",
        )
        controller.activate("work", source="explicit")
        controller.mark_waiting_for_user("pq_1")
        before = controller.goal["blocked_rounds"]
        decision = controller.on_candidate_stop("")
        self.assertEqual(decision["action"], "allow")
        self.assertTrue(decision["waiting_for_user"])
        self.assertEqual(controller.goal["blocked_rounds"], before)
        controller.run_id = "run_2"
        controller.resume_waiting_for_user("pq_1")
        self.assertEqual(controller.goal["status"], "active")
        self.assertEqual(controller.goal["run_id"], "run_2")

    def test_answer_endpoint_enqueues_same_session_and_instance(self):
        item = {
            "question_id": "pq_1",
            "tool_call_id": "call_1",
            "status": "pending",
            "session_id": "sess_1",
            "channel": "group:g1",
            "template_id": "tpl_1",
            "group_id": "g1",
            "instance_id": "inst_1",
            "payload": {"questions": QUESTIONS},
        }
        answered = {
            **item,
            "status": "answered",
            "answer": {
                "answers": [
                    {
                        "question_id": "target",
                        "selected_labels": [],
                        "other_text": "预发布",
                    }
                ]
            },
        }
        coordinator = SimpleNamespace(
            enqueue=lambda session_id, agent_id="": "run_2",
            cancel=lambda run_id: True,
        )
        with patch.object(repos, "get_pending_question", return_value=item), patch.object(
            repos, "answer_pending_question", return_value=answered
        ), patch(
            "server.runtime.run_coordinator.coordinator", coordinator
        ), patch(
            "server.scheduler.group_scheduler.resume_question_async"
        ) as resume:
            # Patch the async publish method with a real awaitable.
            async def publish(*args, **kwargs):
                return None

            with patch("server.api.questions.bus.publish", new=publish):
                result = asyncio.run(
                    answer_question(
                        "pq_1",
                        AnswerBody(
                            answers=[
                                QuestionAnswer(
                                    question_id="target",
                                    selected_labels=[],
                                    other_text="预发布",
                                )
                            ]
                        ),
                    )
                )
        self.assertTrue(result["ok"])
        self.assertEqual(result["runs"], [{"agent_id": "inst_1", "run_id": "run_2"}])
        resume.assert_called_once_with(answered, run_id="run_2")

    def make_turn_runner(self):
        from server.runtime import turn

        sm, state = self.make_state()
        state.messages.append({"role": "user", "content": "Configure the provider"})
        sm.save(state)
        patches = {
            "server.runtime.turn._get_sessions": sm,
            "server.runtime.turn.get_config": {},
            "server.runtime.turn.resolve_template_profile": {"id": "test"},
            "server.runtime.turn._client_for_profile": None,
            "server.runtime.turn._reasoning_kwargs": {},
            "server.runtime.turn._build_goal_evaluator": None,
            "server.runtime.turn._classify_substantial_request": False,
            "server.runtime.turn._build_system_prompt": "Test assistant",
            "server.runtime.turn._resolve_web_enabled": False,
            "server.runtime.feature_flags.flag_enabled": False,
            "server.runtime.feature_flags.validate_feature_flags_at_run_start": None,
            "server.capabilities.catalog.scan_all": {},
            "server.capabilities.catalog.apply_registry": {},
            "server.capabilities.catalog.resolve_bundle": {
                key: [] for key in ("skills", "plugins", "tools", "mcp_servers")
            },
            "server.capabilities.catalog.load_skill_texts": [],
            "server.db.repos.list_capability_registry": [],
            "server.connectors.runtime.resolve_connectors": SimpleNamespace(
                skills={}, cli={}, executable_names=[], skill_index=lambda: []
            ),
            "server.runtime.loop.build_tool_surface": {
                "enabled": False, "schemas": [], "executor": None,
                "mcp_connected": [], "tool_names": [],
                "plugin_prompts": [], "warnings": [],
            },
        }
        for target, value in patches.items():
            self.enterContext(patch(target, return_value=value))

        def run(continuation=None, wait_for=None, fail=False):
            captured = []

            def loop(**kwargs):
                messages = copy.deepcopy(kwargs["messages"])
                captured.extend(messages)
                if fail:
                    raise RuntimeError("model unavailable")
                result = {"reply": "Recorded.", "termination": "normal_stop"}
                if wait_for:
                    messages.append(self.question_tool_call(wait_for))
                    result.update(
                        reply="", termination="waiting_for_user",
                        pending_question=wait_for, api_messages=messages,
                    )
                return result

            with patch("server.runtime.loop.run_tool_loop", side_effect=loop):
                turn._run_chat_turn_inner(
                    scope_key="assistant:test", surface="assistant_dm",
                    user_message="Continue", template={"template_id": "test"},
                    workspace_cwd=str(sm.workdir),
                    continuation=continuation, _session_id=state.meta.session_id,
                )
            return captured

        return sm, state, run

    @staticmethod
    def question_answer(number, text):
        return {
            "question_id": f"pq_{number}", "tool_call_id": f"call_{number}",
            "questions": QUESTIONS,
            "answer": {"answers": [{
                "question_id": "target", "selected_labels": [], "other_text": text,
            }]},
        }

    @staticmethod
    def question_tool_call(question):
        return {"role": "assistant", "content": "", "tool_calls": [{
            "id": question["tool_call_id"], "type": "function",
            "function": {"name": "ask_user_question", "arguments": "{}"},
        }]}

    def seed_question(self, sm, state, question):
        state.context["pending_question_continuation"] = {
            "question_id": question["question_id"],
            "tool_call_id": question["tool_call_id"],
            "tool_messages": [self.question_tool_call(question)],
        }
        sm.save(state)

    def test_answer_survives_completed_turn_and_reaches_next_model_request(self):
        sm, state, run = self.make_turn_runner()
        url = "https://zexitongxue.com/docs/image-api.html"
        answer = self.question_answer(1, url)
        self.seed_question(sm, state, answer)
        resumed = run(answer)
        self.assertEqual(resumed[-1]["role"], "tool")
        self.assertEqual(resumed[-1]["tool_call_id"], "call_1")
        self.assertIn(url, resumed[-1]["content"])
        self.assertFalse(any(url in m.get("content", "") for m in resumed if m["role"] == "user"))
        saved = sm.load(state.meta.session_id)
        self.assertNotIn("pending_question_continuation", saved.context)
        self.assertEqual(sum(url in m.get("content", "") for m in saved.messages), 1)
        next_request = run()
        self.assertEqual(sum(url in m.get("content", "") for m in next_request), 1)

    def test_consecutive_question_answers_are_retained_once(self):
        sm, state, run = self.make_turn_runner()
        first = self.question_answer(1, "https://provider.example/api")
        second = self.question_answer(2, "staging-provider-project")
        self.seed_question(sm, state, first)
        run(first, wait_for=second)
        run(second)
        saved = sm.load(state.meta.session_id)
        next_request = run()
        for text in ("https://provider.example/api", "staging-provider-project"):
            self.assertEqual(sum(text in m.get("content", "") for m in saved.messages), 1)
            self.assertEqual(sum(text in m.get("content", "") for m in next_request), 1)

    def test_invalid_continuation_does_not_persist_answer(self):
        sm, state, run = self.make_turn_runner()
        answer = self.question_answer(1, "https://provider.example/api")
        self.seed_question(sm, state, answer)
        before = copy.deepcopy(sm.load(state.meta.session_id).messages)
        with self.assertRaisesRegex(RuntimeError, "missing or mismatched"):
            run({**answer, "question_id": "wrong_question"})
        self.assertEqual(sm.load(state.meta.session_id).messages, before)

    def test_model_failure_keeps_answer_and_retry_does_not_duplicate_it(self):
        sm, state, run = self.make_turn_runner()
        url = "https://provider.example/retry-api"
        answer = self.question_answer(1, url)
        self.seed_question(sm, state, answer)
        with self.assertRaisesRegex(RuntimeError, "model unavailable"):
            run(answer, fail=True)
        saved = sm.load(state.meta.session_id)
        self.assertEqual(sum(url in m.get("content", "") for m in saved.messages), 1)
        retried = run(answer)
        self.assertEqual(sum(url in m.get("content", "") for m in retried), 1)
        self.assertEqual(retried[-1]["role"], "tool")
        saved = sm.load(state.meta.session_id)
        self.assertEqual(sum(url in m.get("content", "") for m in saved.messages), 1)


if __name__ == "__main__":
    unittest.main()
