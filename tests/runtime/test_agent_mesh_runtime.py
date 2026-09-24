from __future__ import annotations

import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from server.db import mesh_repos as mesh, repos, schema
from server.runtime.context import TurnContext
from server.runtime.executor import ToolExecutor
from server.runtime.loop import run_tool_loop
from server.runtime.mesh_runtime import MeshScheduler, _current_work, permission_request
from server.runtime.tools.mesh import handlers


class MeshRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {"TIGEROSE_HOME": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)
        from avent_paths import reset_path_cache
        reset_path_cache()
        self.addCleanup(reset_path_cache)
        journal = patch("server.runtime.migration_journal.append_journal_event")
        journal.start()
        self.addCleanup(journal.stop)
        schema.init_db()
        from server.db.connection import get_connection
        with get_connection() as conn:
            for name in "ABCD":
                conn.execute("INSERT INTO agent_templates(template_id,name,created_at,updated_at) VALUES (?,?,0,0)", (name, name))
        mesh.configure("A", ["B", "C"])
        mesh.configure("B", ["C", "D"])

    def task(self, **kwargs):
        return mesh.delegate("A", "B", "Analyze evidence", ["Provide sources"], **kwargs)

    def test_group_handoff_requires_explicit_instruction_and_acl(self):
        from server.scheduler.group_scheduler import _enqueue_handoffs_from_reply
        source = {"template_id": "A", "instance_id": "a", "display_name": "A"}
        target = {"template_id": "B", "instance_id": "b", "display_name": "B"}
        with patch("server.scheduler.group_scheduler.repos.find_members_by_mention", return_value=[target]), \
             patch("server.db.mesh_repos.delegate", wraps=mesh.delegate) as delegate, \
             patch("server.scheduler.group_scheduler.repos.add_feed_event", return_value={}) as event, \
             patch("server.scheduler.group_scheduler._publish"):
            def relay(reply, run="run-group", **kwargs):
                _enqueue_handoffs_from_reply("g", from_instance=source, reply=reply,
                    root_user_message="research", hop=0, source_run_id=run, **kwargs)
            relay("感谢 @B 的帮助")
            delegate.assert_not_called()
            relay("@B: research", already_delegated=True)
            delegate.assert_not_called()
            relay("@B: research")
            self.assertEqual(delegate.call_count, 1)
            self.assertEqual(delegate.call_args.kwargs["origin_group_id"], "g")
            mesh.configure("A", [])
            relay("@B: another task", run="run-group-2")
            self.assertEqual(event.call_args.kwargs["meta"]["mesh_error"], "permission_denied")

    def ctx(self, task_id, actor="B", run_id="run-test"):
        return TurnContext(template_id=actor, scope_key=f"mesh:{task_id}:{actor}", session_id=f"session-{actor}",
                           cwd=Path(mesh.task_workspace(task_id)), surface="mesh", mesh_task_id=task_id, run_id=run_id)

    def test_coordination_prompt_covers_all_parallel_outcomes(self):
        from server.runtime.mesh_runtime import _prompt
        task={'task_id':'root','caller_id':'A','target_id':'A',
              'title':'Only write Markdown','objective':'Only write Markdown',
              'outcomes':[{'objective':'Write Markdown','submissions':[{'summary':'8 lines'}]},
                          {'objective':'Guangzhou weather','submissions':[{'summary':'Weather result'}]}]}
        prompt=_prompt({'task_id':'root','agent_id':'A','role':'coordination','task':task})
        payload=json.loads(prompt.split('\n')[-1])['task']
        self.assertEqual(payload['title'],'Summarize all delegated outcomes')
        self.assertIn('every task',payload['objective'])
        self.assertEqual(payload['outcomes'],task['outcomes'])
        self.assertEqual(task['title'],'Only write Markdown')

    def test_worker_submission_review_and_coordination_are_separate(self):
        task = self.task()
        roles = []

        def execute(work):
            roles.append(work["role"])
            task_id = work["task_id"]
            if work["role"] == "receiver":
                mesh.submit(task_id, "B", {"summary": "Evidence and sources"}, "submit-once")
                self.assertEqual(mesh.get_task(task_id)["status"], "submitted")
            elif work["role"] == "caller":
                mesh.review(task_id, "A", 1, "accept", "Sources verified", "review-once")
            return {"reply": "Verified summary", "termination": "normal_stop"}

        scheduler = MeshScheduler(execute=execute)
        for _ in range(3):
            work = mesh.claim_work("test")
            self.assertIsNotNone(work)
            scheduler._run(work)
        self.assertEqual(roles, ["receiver", "caller", "coordination"])
        self.assertEqual(mesh.get_task(task["task_id"])["status"], "completed")
        self.assertEqual(mesh.get_task(task["parent_task_id"])["submissions"][0]["result"]["summary"], "Verified summary")

    def test_worker_acknowledges_actual_stop_after_cancel_fences_result(self):
        task = self.task()
        work = mesh.claim_work("test")

        def execute(_work):
            mesh.cancel_task(task["task_id"], reason="User cancelled")
            return {"reply": "late reply"}

        MeshScheduler(execute=execute)._run(work)
        final = mesh.get_task(task["task_id"])
        self.assertEqual(final["status"], "cancelled")
        self.assertEqual(final["executions_stopping"], 0)
        self.assertEqual(final["submissions"], [])

    def test_coordination_permission_wait_does_not_complete_root(self):
        task = self.task()
        mesh.submit(task["task_id"], "B", {"summary": "done"})
        mesh.review(task["task_id"], "A", 1, "accept")
        work = mesh.claim_work("test")
        self.assertEqual(work["role"], "coordination")

        def execute(work):
            mesh.request_decision(work["task_id"], "A", "user_input", ["answer", "cancel"], {"question": "Which format?"})
            return {"reply": "", "termination": "mesh_yield"}

        MeshScheduler(execute=execute)._run(work)
        self.assertEqual(mesh.get_task(work["task_id"])["status"], "waiting")
        self.assertIsNone(mesh.claim_work("test"))

    def test_coordination_provider_error_is_not_a_final_summary(self):
        task = self.task()
        mesh.submit(task["task_id"], "B", {"summary": "done"})
        mesh.review(task["task_id"], "A", 1, "accept")
        work = mesh.claim_work("test")
        MeshScheduler(execute=lambda _: {"reply": "[LLM error] unavailable", "termination": "llm_error"})._run(work)
        root = mesh.get_task(work["task_id"])
        self.assertNotEqual(root["status"], "completed")
        self.assertEqual(root["submissions"], [])

    def test_coordination_cannot_submit_itself_for_review(self):
        task = self.task()
        mesh.submit(task['task_id'], 'B', {'summary': 'translation'})
        mesh.review(task['task_id'], 'A', 1, 'accept')
        root_id = task['parent_task_id']
        with self.assertRaises(mesh.MeshError):
            mesh.submit(root_id, 'A', {'summary': 'translation'})
        result = handlers(self.ctx(root_id, actor='A'))['submit_agent_task'](
            task_id=root_id, summary='translation', idempotency_key='bad-submit')
        self.assertEqual(result.status, 'failed')
        self.assertFalse(result.metadata.get('mesh_yield'))
        self.assertEqual(mesh.get_task(root_id)['submissions'], [])

    def test_legacy_coordination_submission_can_be_delivered_without_retranslation(self):
        from server.db.connection import get_connection
        source = 'source-recovery'
        with get_connection() as conn:
            conn.execute('INSERT INTO session_fences(session_id) VALUES (?)', (source,))
        task = self.task(origin_session_id=source)
        mesh.submit(task['task_id'], 'B', {'summary': 'accepted translation'})
        mesh.review(task['task_id'], 'A', 1, 'accept')
        root_id = task['parent_task_id']
        with get_connection() as conn:
            conn.execute('INSERT INTO mesh_submissions VALUES (?,1,?,0)', (root_id, json.dumps({'summary': 'accepted translation'})))
            conn.execute('UPDATE mesh_task_bindings SET result_revision=1 WHERE task_id=?', (root_id,))
        mesh.request_decision(root_id, 'A', 'user_decision', ['answer', 'cancel'], {})
        mesh.finish_coordination(root_id, 'A', {'summary': 'accepted translation'})
        root = mesh.get_task(root_id)
        self.assertEqual(root['status'], 'completed')
        self.assertEqual(len(root['submissions']), 2)
        self.assertFalse(any(d['status'] == 'pending' for d in root['decisions']))
        with get_connection() as conn:
            self.assertEqual(conn.execute('SELECT content FROM assistant_messages WHERE session_id=?', (source,)).fetchone()[0], 'accepted translation')

    def test_nested_replay_uses_stable_identity_across_worker_runs(self):
        parent = self.task()
        args = dict(agent_id="C", objective="Read evidence", acceptance_criteria=["Accurate"], idempotency_key="nested-stable")
        first = handlers(self.ctx(parent["task_id"], run_id="run1"))["delegate_agent"](**args)
        second = handlers(self.ctx(parent["task_id"], run_id="run2"))["delegate_agent"](**args)
        self.assertEqual(first.outcome, "ok")
        self.assertEqual(second.outcome, "ok")
        self.assertEqual(json.loads(first.content)["task_id"], json.loads(second.content)["task_id"])

    def test_limit_result_yields_instead_of_executing_more_tools(self):
        task = self.task()
        with patch.object(mesh, "delegate", return_value={"limit_reached": True, "status": "waiting"}):
            result = handlers(self.ctx(task["task_id"]))["delegate_agent"](
                agent_id="C", objective="work", acceptance_criteria=["done"], idempotency_key="once")
        self.assertTrue(result.metadata["mesh_yield"])

    def test_tool_loop_stops_on_submission_without_goal_or_second_call(self):
        task = self.task()
        tools = handlers(self.ctx(task["task_id"]))
        call = SimpleNamespace(id="call-submit", function=SimpleNamespace(name="submit_agent_task", arguments=json.dumps({
            "task_id": task["task_id"], "summary": "Evidence", "idempotency_key": "submit"})))
        response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="", tool_calls=[call]), finish_reason="tool_calls")])
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **_: response)))
        with patch("server.runtime.usage.create_completion", return_value=response) as completion:
            result = run_tool_loop(client=client, model="test", messages=[{"role": "user", "content": "work"}],
                                   schemas=[], executor=ToolExecutor(cwd=Path(self.tmp.name), safe_handlers=tools), max_rounds=4, max_tokens=100)
        self.assertEqual(result["termination"], "mesh_yield")
        self.assertEqual(completion.call_count, 1)
        self.assertEqual(mesh.get_task(task["task_id"])["status"], "submitted")

    def test_permission_wait_is_durable_and_one_use(self):
        task = self.task()
        work = mesh.claim_work("test")
        token = _current_work.set(work)
        self.addCleanup(_current_work.reset, token)
        arguments = dict(tool="write_file", args={"path": "evidence.txt", "content": "data"}, reason="scope_file", detail="Write evidence")
        waiting = permission_request(**arguments)
        decision = waiting["mesh_pending"]
        mesh.finish_work(work["work_id"], work["lease_token"], work["fencing_token"])
        mesh.decide(decision["decision_id"], "approve", decision["expected_task_revision"], idempotency_key="approval")
        resumed = mesh.claim_work("test")
        _current_work.set(resumed)
        approved = permission_request(**arguments)
        self.assertTrue(approved["approved"])
        self.assertIn("mesh_pending", permission_request(**arguments))

    def test_execute_turn_uses_receiver_template_and_authorized_artifact_root(self):
        task = self.task()
        work = mesh.claim_work("test")
        sessions = SimpleNamespace(get_or_create_session_id=lambda *_a, **_kw: "mesh-test-session")
        captured = {}

        def run(**kwargs):
            captured.update(kwargs)
            path = Path(kwargs["workspace_cwd"]) / "evidence.txt"
            path.write_text("actual evidence")
            mesh.register_artifact(task["task_id"], "B", str(path))
            return {"reply": "", "termination": "mesh_yield"}

        with patch("server.runtime.turn._get_sessions", return_value=sessions), patch("server.runtime.turn.run_chat_turn", side_effect=run), \
                patch("server.runtime.run_coordinator.coordinator.enqueue", return_value="run-peer"):
            with mesh.work_context(work["work_id"], work["lease_token"], work["fencing_token"]):
                MeshScheduler._execute_turn(work)
        self.assertEqual(captured["template"]["template_id"], "B")
        self.assertEqual(captured["surface"], "mesh")
        self.assertEqual(captured["workspace_cwd"], mesh.task_workspace(task["task_id"]))
        self.assertEqual(len(mesh.get_task(task["task_id"])["artifacts"]), 1)

    def test_full_turn_runtime_with_fake_provider_returns_summary_to_source(self):
        from openai.types.chat import ChatCompletion
        from session_store import SessionManager
        from server.runtime import turn

        sessions = SessionManager(Path(self.tmp.name) / ".sessions")
        source = sessions.get_or_create_session_id(turn.scope_assistant("A"), "assistant")
        from server.db.connection import get_connection
        with get_connection() as conn:
            conn.execute("INSERT OR IGNORE INTO session_fences(session_id) VALUES (?)", (source,))
        task = self.task(origin_session_id=source)
        calls = []

        def create(**kwargs):
            text = next(m["content"] for m in reversed(kwargs["messages"]) if m["role"] == "user")
            match = re.search(r"task: ([^;]+); role: (\w+)", text)
            self.assertIsNotNone(match)
            task_id, role = match.groups()
            if role == 'coordination':
                self.assertNotIn('submit_agent_task', [t['function']['name'] for t in kwargs['tools']])
            calls.append(role)
            message = {"role": "assistant", "content": "Verified final report"}
            if role in {"receiver", "caller"}:
                name = "submit_agent_task" if role == "receiver" else "review_agent_task"
                args = ({"task_id": task_id, "summary": "Evidence with sources", "idempotency_key": "submission"}
                        if role == "receiver" else {"task_id": task_id, "submission_revision": 1,
                                                   "decision": "accept", "reason": "Verified", "idempotency_key": "review"})
                message = {"role": "assistant", "content": None, "tool_calls": [
                    {"id": "call-" + role, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}
            return ChatCompletion(id="fake-" + role, created=0, model="fake", object="chat.completion",
                                  choices=[{"index": 0, "message": message, "finish_reason": "tool_calls" if role != "coordination" else "stop"}],
                                  usage={"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120})

        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        client.with_options = lambda **_: client
        with patch.object(turn, "_get_sessions", return_value=sessions), \
                patch.object(turn, "resolve_template_profile", return_value={"id": "fake", "label": "fake"}), \
                patch.object(turn, "_client_for_profile", return_value=client):
            scheduler = MeshScheduler()
            for _ in range(3):
                work = mesh.claim_work("full-runtime")
                self.assertIsNotNone(work)
                scheduler._run(work)
        self.assertEqual(calls, ["receiver", "caller", "coordination"])
        self.assertEqual(mesh.get_task(task["task_id"])["status"], "completed")
        from server.db.connection import get_connection
        with get_connection() as conn:
            delivered = conn.execute("SELECT content FROM assistant_messages WHERE session_id=?", (source,)).fetchone()
            self.assertIsNotNone(delivered)
            self.assertEqual(delivered[0], "Verified final report")


if __name__ == "__main__":
    unittest.main()
