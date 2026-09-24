from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from server.connectors.runtime import (
    ConnectorCli,
    ResolvedConnectorSet,
    execute_managed_cli,
    is_managed_cli_command,
    parse_managed_cli,
    resolve_connectors,
)
from server.runtime.context import TurnContext
from server.runtime.loop import build_tool_surface
from server.runtime.tool_semantics import resolve_tool_semantics
from server.runtime.tools.session_ops import get_skill
from server.runtime.tools.registry import TOOL_SPECS


class ConnectorRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cli = ConnectorCli(
            connector_id="dingtalk",
            executable_name="dws",
            executable_path="/managed/dws",
            version="1.0.7",
            skills_dir="",
        )
        self.resolved = ResolvedConnectorSet(cli={"dws": self.cli})

    def test_parser_requires_a_managed_first_argv(self) -> None:
        parsed = parse_managed_cli("dws calendar event list", self.resolved)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed[1], ["calendar", "event", "list"])
        for command in ("env dws calendar", "dws schema calendar | cat", "dws schema calendar && date"):
            self.assertIsNone(parse_managed_cli(command, self.resolved))

    def test_lark_prefix_and_heredoc_are_managed_without_a_shell(self) -> None:
        cli = ConnectorCli("lark", "lark-cli", "/managed/lark-cli", "1.0.0", "")
        resolved = ResolvedConnectorSet(cli={"lark-cli": cli})
        command = (
            "LARKSUITE_CLI_NO_UPDATE_NOTIFIER=1 lark-cli sheets +cells-set "
            "--cells - <<'JSON'\n[{\"value\":\"x\"}]\nJSON"
        )
        self.assertTrue(is_managed_cli_command(command, ("lark-cli",)))
        completed = subprocess.CompletedProcess(["lark-cli"], 0, '{"ok":true}', "")
        with patch("server.connectors.runtime.subprocess.run", return_value=completed) as run:
            result = execute_managed_cli(command, resolved)
        self.assertEqual(result.outcome, "ok")
        self.assertEqual(run.call_args.args[0], ["/managed/lark-cli", "sheets", "+cells-set", "--cells", "-"])
        self.assertEqual(run.call_args.kwargs["input"], '[{"value":"x"}]')
        self.assertNotIn("shell", run.call_args.kwargs)

    def test_lark_legacy_wrapper_is_normalized_without_a_shell(self) -> None:
        cli = ConnectorCli("lark", "lark-cli", "/managed/lark-cli", "1.0.0", "")
        command = (
            'export LARKSUITE_CLI_CONFIG_DIR="/private/config"; '
            'LARK="/managed/lark-cli"; "$LARK" sheets --help 2>&1 | sed -n \'1,50p\''
        )
        self.assertTrue(is_managed_cli_command(command, ("lark-cli",)))
        completed = subprocess.CompletedProcess(["lark-cli"], 0, "help", "")
        with patch("server.connectors.runtime.subprocess.run", return_value=completed) as run:
            result = execute_managed_cli(command, ResolvedConnectorSet(cli={"lark-cli": cli}))
        self.assertEqual(result.outcome, "ok")
        self.assertEqual(run.call_args.args[0], ["/managed/lark-cli", "sheets", "--help"])
        self.assertNotIn("shell", run.call_args.kwargs)

    def test_lark_managed_batch_runs_each_connector_command_without_a_shell(self) -> None:
        cli = ConnectorCli("lark", "lark-cli", "/managed/lark-cli", "1.0.0", "")
        resolved = ResolvedConnectorSet(cli={"lark-cli": cli})
        command = "lark-cli drive +search --help && lark-cli base +title-resolve --help"
        self.assertTrue(is_managed_cli_command(command, ("lark-cli",)))
        completed = [
            subprocess.CompletedProcess(["lark-cli"], 0, "drive help", ""),
            subprocess.CompletedProcess(["lark-cli"], 0, "base help", ""),
        ]
        with patch("server.connectors.runtime.subprocess.run", side_effect=completed) as run:
            result = execute_managed_cli(command, resolved)
        self.assertEqual(result.outcome, "ok")
        self.assertEqual(result.content, "drive help\n\nbase help")
        self.assertTrue(result.metadata["connector_batch"])
        self.assertEqual(run.call_args_list[0].args[0], ["/managed/lark-cli", "drive", "+search", "--help"])
        self.assertEqual(run.call_args_list[1].args[0], ["/managed/lark-cli", "base", "+title-resolve", "--help"])
        self.assertNotIn("shell", run.call_args_list[0].kwargs)

    def test_execution_uses_argv_and_private_connector_config(self) -> None:
        completed = subprocess.CompletedProcess(["dws"], 0, '{"ok":true}', "")
        with patch("server.connectors.runtime.subprocess.run", return_value=completed) as run:
            result = execute_managed_cli("dws calendar event list --format json", self.resolved)
        self.assertEqual(result.outcome, "ok")
        self.assertEqual(
            run.call_args.args[0],
            ["/managed/dws", "calendar", "event", "list", "--format", "json"],
        )
        self.assertNotIn("shell", run.call_args.kwargs)
        self.assertIn("DWS_CONFIG_DIR", run.call_args.kwargs["env"])

    def test_schema_response_is_cached_by_cli_version(self) -> None:
        completed = subprocess.CompletedProcess(["dws"], 0, '{"commands":[]}', "")
        with tempfile.TemporaryDirectory() as tmp, patch(
            "server.connectors.schema_cache.connector_root", return_value=Path(tmp)
        ), patch("server.connectors.runtime.subprocess.run", return_value=completed) as run:
            first = execute_managed_cli("dws schema calendar --compact", self.resolved)
            second = execute_managed_cli("dws schema calendar --compact", self.resolved)
        self.assertEqual(first.metadata["schema_cache"], "miss")
        self.assertEqual(second.metadata["schema_cache"], "hit")
        self.assertEqual(run.call_count, 1)

    def test_resolver_projects_only_enabled_authenticated_connector(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executable = root / "dws"
            executable.touch()
            skill = root / "skills" / "dingtalk-calendar" / "SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_text("# Calendar\n", encoding="utf-8")
            state = {
                "installed": True,
                "authenticated": True,
                "executable": str(executable),
                "active_version": "1.0.7",
                "skills_dir": str(skill.parent.parent),
            }
            with patch("server.connectors.runtime.load_state", return_value=state):
                resolved = resolve_connectors({"connectors": {"dingtalk": {"enabled": True}}})
                disabled = resolve_connectors({"connectors": {"dingtalk": {"enabled": False}}})
        self.assertIn("dws", resolved.cli)
        self.assertIn("connector:dingtalk:skill:dingtalk-calendar", resolved.skills)
        self.assertFalse(disabled.cli)

    def test_resolver_hides_skills_incompatible_with_cli_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executable = root / "dws"
            executable.touch()
            skill = root / "skills" / "dingtalk-calendar" / "SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_text("# Calendar\n", encoding="utf-8")
            state = {
                "installed": True,
                "authenticated": True,
                "executable": str(executable),
                "active_version": "1.0.6",
                "skills_dir": str(skill.parent.parent),
            }
            with patch("server.connectors.runtime.load_state", return_value=state):
                resolved = resolve_connectors({"connectors": {"dingtalk": True}})
        self.assertIn("dws", resolved.cli)
        self.assertFalse(resolved.skills)

    def test_resolver_projects_lark_skills(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executable = root / "lark-cli"
            executable.touch()
            skill = root / "skills" / "lark-calendar" / "SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_text("# Calendar\n", encoding="utf-8")
            state = {
                "installed": True,
                "authenticated": True,
                "executable": str(executable),
                "active_version": "1.0.0",
                "skills_dir": str(skill.parent.parent),
            }
            with patch("server.connectors.runtime.load_state", return_value=state):
                resolved = resolve_connectors({"connectors": {"lark": {"enabled": True}}})
        self.assertIn("lark-cli", resolved.cli)
        self.assertIn("connector:lark:skill:lark-calendar", resolved.skills)

    def test_lark_execution_uses_private_config_and_argv(self) -> None:
        cli = ConnectorCli(
            connector_id="lark",
            executable_name="lark-cli",
            executable_path="/managed/lark-cli",
            version="1.0.0",
            skills_dir="",
        )
        completed = subprocess.CompletedProcess(["lark-cli"], 0, '{"ok":true}', "")
        with patch("server.connectors.runtime.subprocess.run", return_value=completed) as run:
            result = execute_managed_cli("lark-cli calendar +agenda --as user", ResolvedConnectorSet(cli={"lark-cli": cli}))
        self.assertEqual(result.outcome, "ok")
        self.assertEqual(run.call_args.args[0], ["/managed/lark-cli", "calendar", "+agenda", "--as", "user"])
        self.assertNotIn("shell", run.call_args.kwargs)
        self.assertIn("LARKSUITE_CLI_CONFIG_DIR", run.call_args.kwargs["env"])

    def test_lark_execution_reauthorizes_a_missing_user_scope_once(self) -> None:
        cli = ConnectorCli(
            connector_id="lark",
            executable_name="lark-cli",
            executable_path="/managed/lark-cli",
            version="1.0.0",
            skills_dir="",
        )
        missing_scope = subprocess.CompletedProcess(
            ["lark-cli"],
            1,
            '{"error":{"subtype":"missing_scope","missing_scopes":["im:message.send_as_user"]}}',
            "",
        )
        success = subprocess.CompletedProcess(["lark-cli"], 0, '{"ok":true}', "")
        with patch("server.connectors.runtime.subprocess.run", side_effect=[missing_scope, success]), patch(
            "server.connectors.manager.authorize_lark_scopes"
        ) as authorize:
            result = execute_managed_cli("lark-cli im +messages-send --text hello", ResolvedConnectorSet(cli={"lark-cli": cli}))
        self.assertEqual(result.outcome, "ok")
        self.assertEqual(result.metadata["scope_reauthorized"], ["im:message.send_as_user"])
        authorize.assert_called_once_with(["im:message.send_as_user"])

    def test_lark_direct_login_is_completed_by_the_runtime(self) -> None:
        cli = ConnectorCli(
            connector_id="lark",
            executable_name="lark-cli",
            executable_path="/managed/lark-cli",
            version="1.0.0",
            skills_dir="",
        )
        with patch("server.connectors.runtime.subprocess.run") as run, patch(
            "server.connectors.manager.authorize_lark_scopes"
        ) as authorize:
            result = execute_managed_cli(
                "lark-cli auth login --scope 'drive:drive:readonly wiki:wiki:readonly' --no-wait --json",
                ResolvedConnectorSet(cli={"lark-cli": cli}),
            )
        self.assertEqual(result.outcome, "ok")
        self.assertEqual(result.metadata["scope_reauthorized"], ["drive:drive:readonly", "wiki:wiki:readonly"])
        authorize.assert_called_once_with(["drive:drive:readonly", "wiki:wiki:readonly"])
        run.assert_not_called()

    def test_lark_lifecycle_commands_do_not_run_as_bash(self) -> None:
        cli = ConnectorCli("lark", "lark-cli", "/managed/lark-cli", "1.0.0", "")
        with patch("server.connectors.runtime.subprocess.run") as run:
            result = execute_managed_cli(
                "lark-cli config init --new",
                ResolvedConnectorSet(cli={"lark-cli": cli}),
            )
        self.assertEqual(result.outcome, "error")
        self.assertTrue(result.metadata["lifecycle_command"])
        run.assert_not_called()

    def test_resolver_does_not_project_an_unhealthy_connector(self) -> None:
        state = {
            "installed": True,
            "authenticated": True,
            "health": "unhealthy",
            "executable": "/managed/dws",
            "active_version": "1.0.7",
        }
        with patch("server.connectors.runtime.load_state", return_value=state):
            resolved = resolve_connectors({"connectors": {"dingtalk": True}})
        self.assertFalse(resolved.cli)

    def test_connector_skill_reads_by_canonical_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            skill = Path(tmp) / "dingtalk-calendar" / "SKILL.md"
            skill.parent.mkdir()
            skill.write_text("# Calendar instructions", encoding="utf-8")
            skill_id = "connector:dingtalk:skill:dingtalk-calendar"
            ctx = TurnContext(
                template_id="assistant",
                scope_key="scope",
                session_id="session",
                cwd=Path(tmp),
                surface="chat",
                enabled_skill_ids=[skill_id],
                connector_skills={skill_id: skill},
            )
            text = get_skill(ctx, skill_id)
            self.assertIn("# Calendar instructions", text)
            self.assertIn(str(skill.resolve()), text)
            self.assertIn(str(skill.resolve().parent), text)

    def test_tool_surface_preserves_projected_connector_skills(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            skill = Path(tmp) / "lark-im" / "SKILL.md"
            skill.parent.mkdir()
            skill.write_text("# Lark IM instructions", encoding="utf-8")
            skill_id = "connector:lark:skill:lark-im"
            ctx = TurnContext(
                template_id="assistant",
                scope_key="scope",
                session_id="session",
                cwd=Path(tmp),
                surface="chat",
                enabled_skill_ids=[skill_id],
                connector_skills={skill_id: skill},
            )
            with patch(
                "server.runtime.loop.connect_mcp_servers",
                return_value={"clients": {}, "lookup": {}, "warnings": []},
            ), patch("server.runtime.loop.build_handlers", return_value=({}, set())):
                surface = build_tool_surface(bundle={"skills": []}, ctx=ctx)
            self.assertIn("# Lark IM instructions", get_skill(ctx, skill_id))
            self.assertEqual(surface["executor"].skill_roots, (skill.resolve().parent,))

    def test_bash_semantics_are_connector_aware_without_exposing_old_tool(self) -> None:
        sem = resolve_tool_semantics("bash", {"command": "dws calendar +today"})
        self.assertEqual(sem.effect, "read")
        self.assertEqual(sem.goal_role, "query")
        self.assertNotIn("dingtalk_cli", TOOL_SPECS)


if __name__ == "__main__":
    unittest.main()
