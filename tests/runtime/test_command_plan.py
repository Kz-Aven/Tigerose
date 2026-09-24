from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from server.runtime.command_plan import classify_bash_command, task_command_authorization
from server.runtime.executor import ToolExecutor, ToolResult
from server.runtime.loop import run_tool_loop
from server.runtime.scope_guard import build_scope_for_intent, classify_scope


def _tool_call(call_id: str, command: str):
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name="bash", arguments=f'{{"command": "{command}"}}'),
    )


def _client(*calls):
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content="", tool_calls=list(calls)),
                finish_reason="tool_calls",
            )
        ]
    )
    return SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **_kwargs: response))
    )


class CommandPlanTests(unittest.TestCase):
    def test_verified_readonly_chain_is_not_scope_gated(self):
        command = (
            "git status --short && git branch --show-current && git remote -v && "
            "git diff --check && git diff --stat && git diff --cached --stat"
        )
        plan = classify_bash_command(command)
        self.assertEqual(plan.risk, "verified_read")
        self.assertEqual(len(plan.argv_groups), 6)
        self.assertIsNone(
            classify_scope(
                build_scope_for_intent("one_shot_action"),
                "bash",
                {"command": command},
                cwd=".",
            )
        )

    def test_unknown_and_mutation_are_distinct_risk_groups(self):
        self.assertEqual(classify_bash_command("npm run lint").risk, "unknown")
        self.assertEqual(classify_bash_command("touch output.txt").risk, "mutation")
        self.assertEqual(classify_bash_command("rm -rf build").risk, "high_risk")

    def test_unknown_commands_share_one_prompt_and_task_grant(self):
        auth = task_command_authorization({}, workspace=Path.cwd(), continuation=False)
        executed: list[str] = []
        executor = ToolExecutor(
            cwd=Path.cwd(),
            safe_handlers={
                "bash": lambda command, **_kwargs: (
                    executed.append(command) or ToolResult(command, "ok")
                )
            },
        )
        first = _client(_tool_call("one", "tool-a --check"), _tool_call("two", "tool-b --check"))
        second = _client(_tool_call("three", "tool-c --check"))
        decisions: list[dict] = []

        def approve(**kwargs):
            decisions.append(kwargs)
            return {"approved": True, "mode": "always", "domain": kwargs["domain"]}

        scope = build_scope_for_intent("one_shot_action")
        with patch("server.runtime.permissions.request_and_wait", side_effect=approve):
            run_tool_loop(
                client=first,
                model="test",
                messages=[{"role": "user", "content": "inspect"}],
                schemas=[],
                executor=executor,
                max_tokens=100,
                max_rounds=1,
                run_scope=scope,
                permission_channel="assistant:test",
                command_authorization=auth,
            )
            run_tool_loop(
                client=second,
                model="test",
                messages=[{"role": "user", "content": "inspect"}],
                schemas=[],
                executor=executor,
                max_tokens=100,
                max_rounds=1,
                run_scope=build_scope_for_intent("one_shot_action"),
                permission_channel="assistant:test",
                command_authorization=auth,
            )

        self.assertEqual(len(decisions), 1)
        self.assertIn("tool-a --check", decisions[0]["args"]["command"])
        self.assertIn("tool-b --check", decisions[0]["args"]["command"])
        self.assertEqual(executed, ["tool-a --check", "tool-b --check", "tool-c --check"])

    def test_high_risk_command_is_not_granted_to_the_task(self):
        gate = classify_scope(
            build_scope_for_intent("one_shot_action"),
            "bash",
            {"command": "rm -rf build"},
            cwd=".",
        )
        self.assertIsNotNone(gate)
        assert gate is not None
        self.assertEqual(gate["reason"], "scope_bash_high_risk")
        self.assertNotIn("approval_choices", gate)

