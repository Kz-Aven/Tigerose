from __future__ import annotations

import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from server.api.permissions import list_permissions
from server.runtime import permissions
from server.runtime.executor import ToolExecutor, ToolResult
from server.runtime.loop import run_tool_loop
from server.runtime.scope_guard import build_scope_for_intent, classify_scope


class PermissionRecoveryTests(unittest.TestCase):
    def test_enabled_connector_cli_bypasses_bash_permission_policy(self):
        gate = permissions.classify_permission(
            "bash",
            {"command": "lark-cli sheets +workbook-info --help"},
            cwd=Path.cwd(),
            connector_executables=("lark-cli",),
        )
        self.assertIsNone(gate)

    def test_legacy_lark_wrapper_bypasses_bash_permission_policy(self):
        command = (
            'export LARKSUITE_CLI_CONFIG_DIR="/tmp/lark"; '
            'LARK="/tmp/lark-cli"; "$LARK" sheets --help 2>&1 | sed -n \'1,50p\''
        )
        gate = permissions.classify_permission(
            "bash",
            {"command": command},
            cwd=Path.cwd(),
            connector_executables=("lark-cli",),
        )
        self.assertIsNone(gate)
        self.assertIsNone(classify_scope(
            build_scope_for_intent("one_shot_action", connector_executables=["lark-cli"]),
            "bash",
            {"command": command},
            cwd=str(Path.cwd()),
        ))

    def test_unmanaged_or_mixed_cli_still_requires_scope_approval(self):
        for command in (
            "lark-cli sheets +workbook-info --help",
            "lark-cli sheets +workbook-info --help | cat",
        ):
            with self.subTest(command=command):
                gate = classify_scope(
                    build_scope_for_intent(
                        "one_shot_action",
                        connector_executables=() if "|" not in command else ["lark-cli"],
                    ),
                    "bash",
                    {"command": command},
                    cwd=str(Path.cwd()),
                )
                self.assertIsNotNone(gate)

    def test_enabled_connector_cli_batch_bypasses_permission_gates(self):
        command = "lark-cli sheets +workbook-info --help && lark-cli sheets +csv-get --help"
        gate = permissions.classify_permission(
            "bash",
            {"command": command},
            cwd=Path.cwd(),
            connector_executables=("lark-cli",),
        )
        self.assertIsNone(gate)
        self.assertIsNone(classify_scope(
            build_scope_for_intent("one_shot_action", connector_executables=["lark-cli"]),
            "bash",
            {"command": command},
            cwd=str(Path.cwd()),
        ))

    def test_enabled_connector_cli_skips_batch_and_individual_permission_gates(self):
        call = SimpleNamespace(
            id="call_1",
            function=SimpleNamespace(
                name="bash",
                arguments='{"command":"lark-cli sheets +cells-set --cells - <<\u0027JSON\u0027\\n[{\\\"value\\\":\\\"x\\\"}]\\nJSON"}',
            ),
        )
        response = SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content="", tool_calls=[call]),
            finish_reason="tool_calls",
        )])
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
            create=lambda **_kwargs: response,
        )))
        handler = Mock(return_value=ToolResult("ok"))
        with patch("server.runtime.permissions.request_and_wait") as ask:
            run_tool_loop(
                client=client,
                model="test",
                messages=[{"role": "user", "content": "read my Lark sheet"}],
                schemas=[],
                executor=ToolExecutor(
                    cwd=Path.cwd(),
                    all_handlers={"bash": handler},
                    connector_executables=("lark-cli",),
                ),
                max_tokens=100,
                max_rounds=1,
                run_scope=build_scope_for_intent(
                    "one_shot_action", connector_executables=["lark-cli"]
                ),
                permission_channel="assistant:tpl",
                command_authorization={},
            )
        ask.assert_not_called()
        handler.assert_called_once_with(command="lark-cli sheets +cells-set --cells - <<'JSON'\n[{\"value\":\"x\"}]\nJSON")

    def test_channel_listing_recovers_a_request_published_before_subscription(self):
        pending = permissions.PendingPermission(
            request_id="late_request",
            channel="assistant:tpl",
            tool="write_file",
            reason="scope_runtime_domain",
            detail="needs approval",
            domain="runtime.file",
            args_preview={"path": "test.md"},
            timeout_s=120,
            approval_choices=["once", "always"],
        )
        with patch.object(permissions, "_pending", {pending.request_id: pending}):
            response = list_permissions("assistant:tpl")

        self.assertEqual(len(response["items"]), 1)
        item = response["items"][0]
        self.assertEqual(item.pop("age_s"), 0)
        self.assertEqual(item, {
            "request_id": "late_request",
            "tool": "write_file",
            "reason": "scope_runtime_domain",
            "detail": "needs approval",
            "args_preview": {"path": "test.md"},
            "timeout_s": 120,
            "domain": "runtime.file",
            "approval_choices": ["once", "always"],
        })

    def test_resolving_recovered_request_unblocks_waiting_turn(self):
        outcome: dict[str, object] = {}

        def wait_for_permission():
            outcome.update(
                permissions.request_and_wait(
                    channel="assistant:tpl",
                    tool="write_file",
                    args={"path": "test.md"},
                    reason="scope_runtime_domain",
                    detail="needs approval",
                    timeout_s=1,
                    domain="runtime.file",
                    approval_choices="once,always",
                )
            )

        with patch("server.runtime.permissions.scheduler.get_event_loop", return_value=None):
            worker = threading.Thread(target=wait_for_permission)
            worker.start()
            deadline = time.monotonic() + 0.5
            pending: list[dict[str, object]] = []
            while time.monotonic() < deadline and not pending:
                pending = permissions.list_pending("assistant:tpl")
                time.sleep(0.01)
            self.assertEqual(len(pending), 1)
            permissions.resolve(str(pending[0]["request_id"]), True, mode="always")
            worker.join(timeout=0.5)

        self.assertFalse(worker.is_alive())
        self.assertEqual(outcome, {"approved": True, "mode": "always", "domain": "runtime.file"})
        self.assertEqual(permissions.list_pending("assistant:tpl"), [])

    def test_scope_gate_passes_domain_and_choices_to_permission_request(self):
        tool_call = SimpleNamespace(
            id="call_1",
            function=SimpleNamespace(
                name="write_file",
                arguments='{"path":"server/runtime/test.md","content":"x"}',
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
                completions=SimpleNamespace(create=lambda **_kwargs: response)
            )
        )
        captured: dict[str, object] = {}

        def approve(**kwargs):
            captured.update(kwargs)
            return {"approved": True, "mode": "always", "domain": kwargs["domain"]}

        scope = build_scope_for_intent("one_shot_action", user_message="create file")
        executor = ToolExecutor(
            cwd=Path.cwd(),
            safe_handlers={
                "write_file": lambda **_kwargs: ToolResult("done", "success")
            },
        )
        with patch("server.runtime.permissions.request_and_wait", side_effect=approve):
            run_tool_loop(
                client=client,
                model="test",
                messages=[{"role": "user", "content": "create file"}],
                schemas=[],
                executor=executor,
                max_tokens=100,
                max_rounds=1,
                run_scope=scope,
                permission_channel="assistant:tpl",
            )

        self.assertEqual(captured["domain"], "runtime.source_edit")
        self.assertEqual(captured["approval_choices"], "once,always")
