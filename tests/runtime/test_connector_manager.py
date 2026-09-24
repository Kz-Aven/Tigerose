from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from server.connectors import manager


class ConnectorManagerTests(unittest.TestCase):
    def test_lark_connection_requests_the_message_send_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "lark-cli"
            executable.touch()
            state = {"installed": True, "authenticated": False, "executable": str(executable)}
            process = MagicMock()
            saved: list[dict] = []
            with patch("server.connectors.manager.load_state", return_value=state), patch(
                "server.connectors.manager.save_state", side_effect=lambda _id, value: saved.append(dict(value))
            ), patch("server.connectors.manager.subprocess.Popen", return_value=process), patch(
                "server.connectors.manager._lark_json_output",
                side_effect=[
                    {"appId": "cli_lark"},
                    {"verification_url": "https://accounts.feishu.cn/verify", "device_code": "device-code"},
                    {
                        "appId": "cli_lark",
                        "verified": True,
                        "identities": {"user": {"available": True, "userName": "Aven"}},
                    },
                ],
            ) as lark_output:
                manager._connect_lark()  # noqa: SLF001
        self.assertEqual(
            lark_output.call_args_list[1].args[1],
            ["auth", "login", "--scope", "im:message.send_as_user", "--no-wait", "--json"],
        )
        self.assertTrue(saved[-1]["authenticated"])

    def test_lark_skill_export_reads_embedded_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "lark-cli"
            executable.touch()
            destination = Path(tmp) / "skills"

            def lark_output(_executable: Path, args: list[str], timeout: int = 30) -> dict:
                responses = {
                    ("skills", "list"): {"skills": [{"name": "lark-doc"}]},
                    ("skills", "list", "lark-doc"): {
                        "entries": [
                            {"path": "lark-doc/SKILL.md", "is_dir": False},
                            {"path": "lark-doc/references", "is_dir": True},
                        ]
                    },
                    ("skills", "list", "lark-doc/references"): {
                        "entries": [{"path": "lark-doc/references/fetch.md", "is_dir": False}]
                    },
                    ("skills", "read", "lark-doc/SKILL.md", "--json"): {"content": "# Lark Doc\n"},
                    ("skills", "read", "lark-doc/references/fetch.md", "--json"): {"content": "# Fetch\n"},
                }
                return responses[tuple(args)]

            with patch("server.connectors.manager._lark_json_output", side_effect=lark_output):
                manager._export_lark_skills(executable, destination)  # noqa: SLF001

            self.assertEqual((destination / "lark-doc" / "SKILL.md").read_text(encoding="utf-8"), "# Lark Doc\n")
            self.assertEqual(
                (destination / "lark-doc" / "references" / "fetch.md").read_text(encoding="utf-8"), "# Fetch\n"
            )

    def test_lark_status_reads_verified_user_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "lark-cli"
            executable.touch()
            state = {"installed": True, "authenticated": False, "executable": str(executable)}
            saved: list[dict] = []
            payload = {
                "appId": "cli_lark",
                "brand": "feishu",
                "identity": "user",
                "verified": True,
                "identities": {
                    "bot": {"appName": "Tigerose 飞书"},
                    "user": {"available": True, "userName": "Aven"},
                },
            }
            with patch("server.connectors.manager.load_state", return_value=state), patch(
                "server.connectors.manager.save_state", side_effect=lambda _id, value: saved.append(dict(value))
            ), patch("server.connectors.manager._lark_json_output", return_value=payload):
                status = manager.status("lark")
        self.assertTrue(status.authenticated)
        self.assertEqual(status.account_name, "Aven")
        self.assertEqual(status.corp_name, "feishu")
        self.assertEqual(status.app_id, "cli_lark")
        self.assertEqual(status.app_name, "Tigerose 飞书")
        self.assertTrue(saved[-1]["configured"])
        self.assertEqual(saved[-1]["app_id"], "cli_lark")
        self.assertEqual(saved[-1]["app_name"], "Tigerose 飞书")

    def test_connect_polls_authorization_status_instead_of_waiting_for_login_exit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "dws"
            executable.touch()
            state = {"installed": True, "authenticated": False, "executable": str(executable)}
            process = MagicMock()
            process.poll.return_value = None
            saved: list[dict] = []
            with patch("server.connectors.manager.load_state", return_value=state), patch(
                "server.connectors.manager.save_state", side_effect=lambda _id, value: saved.append(dict(value))
            ), patch("server.connectors.manager.subprocess.Popen", return_value=process), patch(
                "server.connectors.manager._json_output",
                side_effect=[
                    {"authenticated": False},
                    {"authenticated": True, "user_name": "Aven", "corp_name": "Tigerose"},
                ],
            ), patch("server.connectors.manager.time.sleep"):
                manager._connect_dingtalk()  # noqa: SLF001
        self.assertTrue(saved[-1]["authenticated"])
        self.assertEqual(saved[-1]["account_name"], "Aven")
        process.terminate.assert_called_once()

    def test_status_repairs_a_stale_authorization_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "dws"
            executable.touch()
            state = {"installed": True, "authenticated": False, "executable": str(executable)}
            saved: list[dict] = []
            with patch("server.connectors.manager.load_state", return_value=state), patch(
                "server.connectors.manager.save_state", side_effect=lambda _id, value: saved.append(dict(value))
            ), patch(
                "server.connectors.manager._json_output",
                return_value={"authenticated": True, "user_name": "Aven", "corp_name": "Tigerose"},
            ):
                status = manager.status("dingtalk")
        self.assertTrue(status.authenticated)
        self.assertTrue(saved[-1]["authenticated"])


if __name__ == "__main__":
    unittest.main()
