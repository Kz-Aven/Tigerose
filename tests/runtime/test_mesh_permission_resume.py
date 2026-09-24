from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from server.db import mesh_repos as mesh, repos, schema
from server.runtime.command_plan import task_command_authorization
from server.runtime.executor import ToolExecutor, ToolResult
from server.runtime.loop import run_tool_loop
from server.runtime.mesh_runtime import _current_work
from server.runtime.scope_guard import build_scope_for_intent


class MeshPermissionResumeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {"TIGEROSE_HOME": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)
        journal = patch("server.runtime.migration_journal.append_journal_event")
        journal.start()
        self.addCleanup(journal.stop)
        from avent_paths import reset_path_cache
        reset_path_cache()
        self.addCleanup(reset_path_cache)
        schema.init_db()
        self.a, self.b = [repos.create_template(name=name)["template_id"] for name in ("A", "B")]
        mesh.configure(self.a, [self.b])
        self.task = mesh.delegate(self.a, self.b, "Inspect evidence", ["accurate"], idempotency_key="task")
        self.workspace = Path(mesh.task_workspace(self.task["task_id"]))
        self.workspace.mkdir(parents=True)
        self.dispatched = []
        self.executor = ToolExecutor(cwd=self.workspace, safe_handlers={"bash": self.fake_bash})

    def fake_bash(self, **arguments):
        # Intentionally never invokes a shell, a model, or any external service.
        self.dispatched.append(arguments)
        return ToolResult("42", "ok")

    def test_background_wait_reads_live_state_after_repeated_identical_calls(self):
        for tool in ('bash_wait', 'bash_status'):
            executions = []
            def poll(job_id):
                executions.append(job_id)
                return ToolResult(json.dumps({'status': 'completed' if len(executions) == 4 else 'running'}), 'ok')
            call = SimpleNamespace(id='poll', function=SimpleNamespace(name=tool, arguments='{"job_id":"job-test"}'))
            polling = SimpleNamespace(usage=None, choices=[SimpleNamespace(message=SimpleNamespace(content='', tool_calls=[call]), finish_reason='tool_calls')])
            done = SimpleNamespace(usage=None, choices=[SimpleNamespace(message=SimpleNamespace(content='done', tool_calls=[]), finish_reason='stop')])
            messages = [{'role': 'user', 'content': 'Wait for existing job'}]
            with patch('server.runtime.usage.create_completion', side_effect=[polling] * 4 + [done]):
                run_tool_loop(client=SimpleNamespace(), model='fake', messages=messages, schemas=[],
                    executor=ToolExecutor(cwd=self.workspace, safe_handlers={tool: poll}), max_rounds=5, max_tokens=100)
            self.assertEqual(executions, ['job-test'] * 4)

    def run_call(self, work, arguments):
        call = SimpleNamespace(id="call-original", function=SimpleNamespace(name="bash", arguments=json.dumps(arguments)))
        response = SimpleNamespace(usage=SimpleNamespace(prompt_tokens=20, completion_tokens=10),
            choices=[SimpleNamespace(message=SimpleNamespace(content="", tool_calls=[call]), finish_reason="tool_calls")])
        token = _current_work.set(work)
        try:
            with mesh.work_context(work["work_id"], work["lease_token"], work["fencing_token"]), \
                 patch("server.runtime.usage.create_completion", return_value=response):
                return run_tool_loop(
                    client=SimpleNamespace(), model="fake", messages=[{"role": "user", "content": "Inspect evidence"}],
                    schemas=[], executor=self.executor, max_tokens=100, max_rounds=1,
                    run_scope=build_scope_for_intent("one_shot_action"), permission_channel="mesh:test",
                    command_authorization=task_command_authorization({}, workspace=self.workspace, continuation=False),
                )
        finally:
            _current_work.reset(token)

    def test_original_bash_arguments_survive_permission_and_resume_once(self):
        arguments = {"command": "python3 -c 'print(42)'", "timeout": 45, "run_in_background": False}
        first_work = mesh.claim_work("worker")
        result = self.run_call(first_work, arguments)
        self.assertEqual(result["termination"], "mesh_yield")
        self.assertEqual(self.dispatched, [])
        decisions = mesh.get_task(self.task["task_id"])["decisions"]
        self.assertEqual(len(decisions), 1)
        decision = decisions[0]
        self.assertEqual(decision["reason"], "permission")
        self.assertEqual(decision["payload"]["tool"], "bash")
        self.assertEqual(decision["payload"]["args"], arguments)
        self.assertFalse(decision["payload"]["args"]["command"].startswith("1. "))
        mesh.finish_work(first_work["work_id"], first_work["lease_token"], first_work["fencing_token"])
        self.assertIsNone(mesh.claim_work("worker"))

        mesh.decide(decision["decision_id"], "approve", decision["expected_task_revision"], idempotency_key="user-approval")
        resumed_work = mesh.claim_work("worker")
        self.assertIsNotNone(resumed_work)
        self.run_call(resumed_work, arguments)
        self.assertEqual(self.dispatched, [arguments])
        after = mesh.get_task(self.task["task_id"])["decisions"]
        self.assertEqual(len(after), 1)
        self.assertEqual(after[0]["status"], "resolved")
        self.assertEqual(after[0]["answer"]["consumed_by"], resumed_work["work_id"])
        self.assertEqual(mesh.pending_permission_decisions(), {"items": []})

    def test_approved_background_bash_reaches_real_handler_and_finishes(self):
        from server.runtime.context import TurnContext
        from server.runtime.tools.handlers import build_handlers
        from server.runtime.background_bash import cleanup_scope
        ctx = TurnContext(template_id=self.b, scope_key='mesh-background-test', cwd=self.workspace, session_id='test-background', surface='mesh')
        self.addCleanup(cleanup_scope, ctx.scope_key, ctx.template_id)
        handlers, _ = build_handlers(ctx, allow_names=['bash', 'bash_wait'])
        results = []
        def execute(**args):
            result = handlers['bash'](**args)
            results.append(result)
            return result
        self.executor = ToolExecutor(cwd=self.workspace, safe_handlers={'bash': execute})
        arguments = {'command': "python3 -c 'print(42)'", 'run_in_background': True}
        work = mesh.claim_work('worker')
        self.assertEqual(self.run_call(work, arguments)['termination'], 'mesh_yield')
        self.assertEqual(results, [])
        decision = mesh.get_task(self.task['task_id'])['decisions'][0]
        self.assertEqual(decision['payload']['args'], arguments)
        mesh.finish_work(work['work_id'], work['lease_token'], work['fencing_token'])
        mesh.decide(decision['decision_id'], 'approve', decision['expected_task_revision'])
        self.run_call(mesh.claim_work('worker'), arguments)
        self.assertEqual(len(results), 1)
        job = json.loads(results[0].content)
        final = json.loads(handlers['bash_wait'](job['job_id'], timeout_s=5).content)
        self.assertEqual(final['status'], 'completed')
        self.assertEqual(final['exit_code'], 0)
        self.assertIn('42', final['output'])
        self.assertEqual(mesh.pending_permission_decisions(), {'items': []})


if __name__ == "__main__":
    unittest.main()
