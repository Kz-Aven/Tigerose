from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from server.runtime.executor import ToolExecutor
from server.runtime.loop import run_tool_loop
from server.runtime.permissions import classify_permission
from server.runtime.policy import PermissionPolicy
from server.runtime.scope_guard import build_scope_for_intent, classify_scope
from server.runtime.tool_authorization import explicitly_requested
from server.runtime.tools import worktree


class ConditionalAuthorizationTests(unittest.TestCase):
    def test_loop_only_skips_confirmation_when_exact_request_matches(self):
        for name in ("remember", "schedule_cron", "cancel_cron", "create_worktree"):
            for matched in (True, False):
                with self.subTest(name=name, matched=matched):
                    call = SimpleNamespace(id="call_1", function=SimpleNamespace(
                        name=name, arguments='{}',
                    ))
                    response = SimpleNamespace(choices=[SimpleNamespace(
                        message=SimpleNamespace(content="", tool_calls=[call]),
                        finish_reason="tool_calls",
                    )])
                    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
                        create=lambda **kwargs: response,
                    )))
                    handler = Mock(return_value="done")
                    with patch("server.runtime.tool_authorization.explicitly_requested", return_value=matched) as match, \
                         patch("server.runtime.permissions.request_and_wait", return_value={"approved": False}) as ask:
                        run_tool_loop(
                            client=client, model="test", messages=[], schemas=[],
                            executor=ToolExecutor(cwd=Path.cwd(), safe_handlers={name: handler}),
                            max_tokens=100, max_rounds=1,
                            run_scope=build_scope_for_intent("one_shot_action", user_message="User request"),
                            permission_channel="assistant:test",
                        )
                    self.assertEqual(match.call_args.args[:3], ("User request", name, {}))
                    self.assertEqual(handler.call_count, int(matched))
                    self.assertEqual(ask.call_count, int(not matched))

    def test_edit_permission_does_not_depend_on_run_scope_or_full_file_access(self):
        with tempfile.TemporaryDirectory() as tmp:
            for path in (str(Path(tmp) / "report.txt"), "/outside/report.txt"):
                gate = classify_permission("edit_file", {"path": path}, cwd=Path(tmp),
                                           policy=PermissionPolicy(file_access="full"))
                self.assertEqual(gate["action"], "ask")
                self.assertEqual(gate["approval_choices"], "once")

    def check_request(self, request, name, args, decision):
        response = SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=decision),
        )])
        with patch("server.runtime.usage.create_completion", return_value=response) as complete:
            result = explicitly_requested(
                request, name, args, client=Mock(), model="test", usage_context={},
            )
        return result, complete

    def test_conditional_tools_require_request_instead_of_runtime_domain_grant(self):
        scope = build_scope_for_intent("durable_goal")
        scope.allow_runtime_edit = True
        for name in ("remember", "create_worktree", "schedule_cron", "cancel_cron"):
            with self.subTest(name=name):
                gate = classify_scope(scope, name, {})
                self.assertEqual(gate["reason"], "scope_explicit_request")

    def test_exact_call_is_sent_to_matcher_and_only_boolean_true_allows(self):
        cases = [
            ("Remember I prefer Python", "remember", {"body": "I prefer Python"}),
            ("Implement the fix", "create_worktree", {"name": "fix"}),
            ("Remind me in 60 seconds", "schedule_cron", {"prompt": "Reminder", "delay_seconds": 60}),
            ("Cancel job_123", "cancel_cron", {"job_id": "job_123"}),
        ]
        for request, name, args in cases:
            for raw, expected in [(' {"authorized": true}', True), ('{"authorized": false}', False),
                                  ('{"authorized": "true"}', False), ('[]', False), ('invalid', False)]:
                with self.subTest(name=name, raw=raw):
                    result, complete = self.check_request(request, name, args, raw)
                    self.assertIs(result, expected)
                    payload = json.loads(complete.call_args.kwargs["messages"][1]["content"])
                    self.assertEqual(payload, {"user_request": request, "tool": name, "arguments": args})

    def test_missing_request_and_unsupported_tools_never_call_matcher(self):
        for request, name, args in [
            ("", "remember", {}), ("run it", "bash", {}),
            ("edit", "edit_file", {}), ("cancel my reminder", "cancel_cron", {"job_id": "unmentioned"}),
        ]:
            result, complete = self.check_request(request, name, args, '{"authorized": true}')
            self.assertFalse(result)
            complete.assert_not_called()

    def test_classifier_failure_keeps_confirmation(self):
        with patch("server.runtime.usage.create_completion", side_effect=TimeoutError):
            self.assertFalse(explicitly_requested(
                "Remember this", "remember", {"body": "this"},
                client=Mock(), model="test", usage_context={},
            ))

    def test_plan_review_only_bypasses_scope_for_authorized_collaboration(self):
        scope = build_scope_for_intent("one_shot_action")
        self.assertEqual(classify_scope(scope, "review_plan", {})["action"], "ask")
        scope.allow_spawn = True
        self.assertIsNone(classify_scope(scope, "review_plan", {}))
        self.assertFalse(scope.allow_runtime_edit)

    def test_worktree_does_not_bind_foreign_task(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(worktree, "_worktrees_dir", return_value=Path(tmp)), \
             patch.object(worktree.task_board, "get_task", return_value={"scope_key": "other"}), \
             patch.object(worktree, "_run_git") as git, \
             patch.object(worktree.task_board, "bind_worktree") as bind:
            result = worktree.create_worktree("fix", "task_1", expected_scope="current")
        self.assertIn("scope mismatch", result)
        git.assert_not_called()
        bind.assert_not_called()
