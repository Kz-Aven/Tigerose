"""Enabled skill resources preserve the surrounding file permission boundary."""

import tempfile
import unittest
import json
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from server.runtime.permissions import classify_permission
from server.runtime.executor import ToolExecutor
from server.runtime.loop import run_tool_loop
from server.runtime.policy import PermissionPolicy
from server.runtime.tools import files
from server.runtime.tools.files import skill_script_argument_index


class SkillResourcePermissionsTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.skill = self.root / "enabled skill"
        self.skill.mkdir()
        self.script = self.skill / "generate.py"
        self.script.write_text("print('ok')\n", encoding="utf-8")
        self.policy = PermissionPolicy(file_access="workspace")

    def classify(self, name, args, roots=None):
        return classify_permission(
            name, args, cwd=self.workspace, policy=self.policy,
            skill_roots=(self.skill,) if roots is None else roots,
        )

    def test_enabled_resource_read_is_allowed_for_this_call(self):
        for tool in ("read_file", "excel_read"):
            with self.subTest(tool=tool):
                gate = self.classify(tool, {"path": str(self.script)})
                self.assertEqual(gate["action"], "allow")
                self.assertEqual(gate["reason"], "skill_resource_read")
                self.assertEqual(gate["allow_external"], "true")

    def test_disabled_resource_and_skill_writes_still_denied(self):
        gate = self.classify("read_file", {"path": str(self.script)}, roots=())
        self.assertEqual(gate["action"], "deny")
        for tool in ("write_file", "edit_file", "excel_write"):
            with self.subTest(tool=tool):
                gate = self.classify(tool, {"path": str(self.script)})
                self.assertEqual(gate["action"], "deny")

    def test_loop_reads_real_skill_file_and_resets_elevation(self):
        call = SimpleNamespace(id="skill-read", function=SimpleNamespace(
            name="read_file", arguments=json.dumps({"path": str(self.script)}),
        ))
        response = SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content="", tool_calls=[call]), finish_reason="tool_calls",
        )])
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
            create=lambda **kwargs: response,
        )))
        executor = ToolExecutor(
            cwd=self.workspace, skill_roots=(self.skill,),
            safe_handlers={"read_file": partial(files.read_file, self.workspace)},
        )
        observed = []
        execute = executor.execute

        def record(*args, **kwargs):
            result = execute(*args, **kwargs)
            observed.append(result)
            return result

        with patch("server.runtime.permissions.request_and_wait") as approval, patch.object(executor, "execute", side_effect=record):
            run_tool_loop(
                client=client, model="test", messages=[{"role": "user", "content": "Read skill resources"}],
                schemas=[], executor=executor, max_tokens=100, max_rounds=1,
                permission_channel="assistant:skill-test", permission_policy=self.policy,
            )
        approval.assert_not_called()
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0].outcome, "ok")
        with self.assertRaises(PermissionError):
            files.ensure_under(self.workspace, self.script)

    def test_normalized_traversal_and_symlink_escape_denied(self):
        outside = self.root / "outside.py"
        outside.write_text("", encoding="utf-8")
        link = self.skill / "linked.py"
        link.symlink_to(outside)
        for path in (link, self.skill / ".." / "outside.py"):
            with self.subTest(path=path):
                self.assertEqual(self.classify("read_file", {"path": str(path)})["action"], "deny")
                self.assertEqual(self.classify("bash", {"command": f'python3 "{path}"'})["action"], "deny")

    def test_single_supported_script_entry_is_exempt(self):
        for interpreter in ("python", "python3", "python3 -u"):
            with self.subTest(interpreter=interpreter):
                self.assertIsNone(self.classify("bash", {"command": f'{interpreter} "{self.script}" --output result.png'}))
        for suffix in (".js", ".mjs", ".cjs"):
            script = self.skill / f"generate{suffix}"
            script.write_text("", encoding="utf-8")
            self.assertIsNone(self.classify("bash", {"command": f'node "{script}"'}))

    def test_other_external_arguments_remain_denied(self):
        for argument in (
            f'--output "{self.root / "out.png"}"',
            f'--output="{self.script}"',
            "--output ../out.png",
            "--output=results/../../out.png",
        ):
            with self.subTest(argument=argument):
                gate = self.classify("bash", {"command": f'python3 "{self.script}" {argument}'})
                self.assertEqual(gate["action"], "deny")
                self.assertEqual(gate["reason"], "external_path")

    def test_shell_expansion_and_interpreter_modes_are_not_exempt(self):
        for command in (
            f'python3 "{self.script}"; echo done',
            f'python3 "{self.script}" && echo done',
            f'python3 "{self.script}" > result.txt',
            f'python3 "{self.script}" "$(pwd)"',
            f'python3 "{self.script}" "$HOME"',
            f'python3 -c "{self.script}"',
            f'python3 -m "{self.script}"',
            f'bash "{self.script}"',
        ):
            with self.subTest(command=command):
                self.assertIsNone(skill_script_argument_index(command, cwd=self.workspace, skill_roots=(self.skill,)))
                self.assertEqual(self.classify("bash", {"command": command})["action"], "deny")

    def test_missing_script_is_not_exempt(self):
        command = f'python3 "{self.skill / "missing.py"}"'
        self.assertEqual(self.classify("bash", {"command": command})["action"], "deny")

    def test_dangerous_command_check_still_runs_first(self):
        command = f'python3 "{self.script}"; sudo reboot'
        gate = self.classify("bash", {"command": command})
        self.assertEqual(gate["action"], "deny")
        self.assertEqual(gate["reason"], "dangerous_bash")


if __name__ == "__main__":
    unittest.main()
