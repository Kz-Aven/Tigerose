from __future__ import annotations

import subprocess
import unittest
from unittest.mock import patch

from server.connectors import dingtalk_cli
from server.runtime.tool_semantics import resolve_tool_semantics


class DingTalkConnectorTests(unittest.TestCase):
    def test_status_reads_only_sanitized_identity(self) -> None:
        completed = subprocess.CompletedProcess(
            ["dws"],
            0,
            '{"authenticated":true,"user_name":"Aven","corp_name":"Tigerose","corp_id":"corp","user_id":"user"}',
            "",
        )
        with patch.object(dingtalk_cli.subprocess, "run", return_value=completed):
            status = dingtalk_cli._status_for("/usr/local/bin/dws")  # noqa: SLF001
        self.assertTrue(status.connected)
        self.assertEqual(status.account_name, "Aven")
        self.assertEqual(status.corp_name, "Tigerose")
        self.assertEqual(status.profile, "corp:user")

    def test_tool_uses_argv_not_a_shell_command(self) -> None:
        current = dingtalk_cli.DwsStatus(
            executable="/usr/local/bin/dws", installed=True, connected=True, state="connected"
        )
        completed = subprocess.CompletedProcess(["dws"], 0, '{"ok":true}', "")
        with patch.object(dingtalk_cli, "status", return_value=current), patch.object(
            dingtalk_cli.subprocess, "run", return_value=completed
        ) as run:
            result = dingtalk_cli.run_cli(["calendar", "event", "list", "--format", "json"])
        self.assertEqual(result.outcome, "ok")
        self.assertEqual(
            run.call_args.args[0],
            ["/usr/local/bin/dws", "calendar", "event", "list", "--format", "json"],
        )
        self.assertNotIn("shell", run.call_args.kwargs)

    def test_tool_requires_connected_account(self) -> None:
        with patch.object(dingtalk_cli, "status", return_value=dingtalk_cli.DwsStatus()):
            result = dingtalk_cli.run_cli(["calendar", "event", "list"])
        self.assertEqual(result.outcome, "denied")

    def test_tool_rejects_invalid_argv(self) -> None:
        current = dingtalk_cli.DwsStatus(
            executable="/usr/local/bin/dws", installed=True, connected=True, state="connected"
        )
        with patch.object(dingtalk_cli, "status", return_value=current):
            result = dingtalk_cli.run_cli(["chat\x00message"])
        self.assertEqual(result.outcome, "error")

    def test_calendar_create_returns_matching_acceptance_receipt(self) -> None:
        current = dingtalk_cli.DwsStatus(
            executable="/usr/local/bin/dws", installed=True, connected=True, state="connected"
        )
        completed = subprocess.CompletedProcess(
            ["dws"], 0, '{"result":{"eventId":"event_123"}}', ""
        )
        with patch.object(dingtalk_cli, "status", return_value=current), patch.object(
            dingtalk_cli.subprocess, "run", return_value=completed
        ):
            result = dingtalk_cli.run_cli(
                ["calendar", "event", "create", "--title", "讨论", "--start", "2026-09-01T10:00:00+08:00"]
            )
        self.assertEqual(result.outcome, "ok")
        self.assertEqual(result.metadata["acceptance_receipt"]["event_id"], "event_123")
        self.assertEqual(result.metadata["acceptance_receipt"]["calendar_id"], "primary")

    def test_write_error_is_in_doubt_and_never_a_plain_failure(self) -> None:
        current = dingtalk_cli.DwsStatus(
            executable="/usr/local/bin/dws", installed=True, connected=True, state="connected"
        )
        completed = subprocess.CompletedProcess(
            ["dws"], 1, '{"error":"readback_attendee_missing"}', ""
        )
        with patch.object(dingtalk_cli, "status", return_value=current), patch.object(
            dingtalk_cli.subprocess, "run", return_value=completed
        ):
            result = dingtalk_cli.run_cli(["calendar", "+book", "--title", "讨论"])
        self.assertEqual(result.outcome, "in_doubt")
        self.assertTrue(result.metadata["no_replay"])

    def test_known_queries_and_unknown_commands_are_not_generic_writes(self) -> None:
        query = resolve_tool_semantics("dingtalk_cli", {"args": ["calendar", "+today"]})
        unknown = resolve_tool_semantics("dingtalk_cli", {"args": ["future", "new-command"]})
        self.assertEqual(query.goal_role, "query")
        self.assertEqual(query.effect, "read")
        self.assertEqual(unknown.goal_role, "unknown")
        self.assertEqual(unknown.effect, "unknown")

    def test_schema_is_not_treated_as_a_write_receipt(self) -> None:
        current = dingtalk_cli.DwsStatus(
            executable="/usr/local/bin/dws", installed=True, connected=True, state="connected"
        )
        completed = subprocess.CompletedProcess(["dws"], 0, '{"id":"not-a-receipt"}', "")
        with patch.object(dingtalk_cli, "status", return_value=current), patch.object(
            dingtalk_cli.subprocess, "run", return_value=completed
        ):
            result = dingtalk_cli.run_cli(["schema"])
        self.assertNotIn("acceptance_receipt", result.metadata)

    def test_unknown_command_validation_is_a_confirmed_no_side_effect_failure(self) -> None:
        current = dingtalk_cli.DwsStatus(
            executable="/usr/local/bin/dws", installed=True, connected=True, state="connected"
        )
        completed = subprocess.CompletedProcess(
            ["dws"], 3, '{"error":{"category":"validation","message":"unknown command"}}', ""
        )
        with patch.object(dingtalk_cli, "status", return_value=current), patch.object(
            dingtalk_cli.subprocess, "run", return_value=completed
        ):
            result = dingtalk_cli.run_cli(["contact", "search-user", "--keyword", "贵权"])
        self.assertEqual(result.outcome, "error")


if __name__ == "__main__":
    unittest.main()
