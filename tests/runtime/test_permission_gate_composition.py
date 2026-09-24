from __future__ import annotations

import json
import tempfile
import unittest
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from server.runtime.executor import ToolExecutor
from server.runtime.loop import run_tool_loop
from server.runtime.policy import PermissionPolicy
from server.runtime.scope_guard import build_scope_for_intent
from server.runtime.tools import excel, files


class PermissionGateCompositionTests(unittest.TestCase):
    def setUp(self):
        self.workspace_dir = tempfile.TemporaryDirectory()
        self.external_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.workspace_dir.cleanup)
        self.addCleanup(self.external_dir.cleanup)
        self.workspace = Path(self.workspace_dir.name)
        self.external = Path(self.external_dir.name)

    def run_call(self, name, args, *, file_access="ask", approved=True):
        tool_call = SimpleNamespace(
            id="call_1",
            function=SimpleNamespace(name=name, arguments=json.dumps(args)),
        )
        response = SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content="", tool_calls=[tool_call]),
            finish_reason="tool_calls",
        )])
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
            create=lambda **_kwargs: response,
        )))
        executor = ToolExecutor(
            cwd=self.workspace,
            dangerous_handlers={
                "write_file": partial(files.write_file, self.workspace),
                "edit_file": partial(files.edit_file, self.workspace),
                "excel_write": partial(excel.excel_write, self.workspace),
            },
        )
        events = []
        with patch("server.runtime.permissions.request_and_wait", return_value={
            "approved": approved, "mode": "once", "domain": "",
        }) as request, patch.object(executor, "execute", wraps=executor.execute) as execute:
            result = run_tool_loop(
                client=client,
                model="test",
                messages=[{"role": "user", "content": "Update the file"}],
                schemas=[],
                executor=executor,
                max_tokens=100,
                max_rounds=1,
                run_scope=build_scope_for_intent("one_shot_action"),
                permission_channel="assistant:permission-test",
                permission_policy=PermissionPolicy(file_access=file_access),
                on_event=events.append,
            )
        return request, execute, events, result

    def external_call(self, name):
        target = self.external / "server" / "runtime" / "target.txt"
        if name == "edit_file":
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("original", encoding="utf-8")
            args = {"path": str(target), "old_text": "original", "new_text": "updated"}
        else:
            args = {"path": str(target), "content": "updated"}
        return target, args

    def test_workspace_policy_denies_external_targets_despite_scope_ask(self):
        for name in ("write_file", "edit_file"):
            with self.subTest(tool=name):
                target, args = self.external_call(name)
                request, execute, _, _ = self.run_call(name, args, file_access="workspace")
                request.assert_not_called()
                execute.assert_not_called()
                if name == "edit_file":
                    self.assertEqual(target.read_text(encoding="utf-8"), "original")
                else:
                    self.assertFalse(target.exists())

    def test_combined_approval_enables_real_external_write_and_edit(self):
        for name in ("write_file", "edit_file"):
            with self.subTest(tool=name):
                target, args = self.external_call(name)
                request, execute, _, _ = self.run_call(name, args)
                request.assert_called_once()
                execute.assert_called_once()
                self.assertEqual(target.read_text(encoding="utf-8"), "updated")
                with self.assertRaises(PermissionError):
                    files.ensure_under(self.workspace, target)

    def test_combined_rejection_does_not_write_or_dispatch(self):
        for name in ("write_file", "edit_file"):
            with self.subTest(tool=name):
                target, args = self.external_call(name)
                request, execute, _, _ = self.run_call(name, args, approved=False)
                request.assert_called_once()
                execute.assert_not_called()
                if name == "edit_file":
                    self.assertEqual(target.read_text(encoding="utf-8"), "original")
                else:
                    self.assertFalse(target.exists())

    def test_ordinary_write_runs_without_permission_request(self):
        target = self.workspace / "report.txt"
        request, execute, events, _ = self.run_call(
            "write_file", {"path": str(target), "content": "updated"},
        )
        request.assert_not_called()
        execute.assert_called_once()
        self.assertEqual(target.read_text(encoding="utf-8"), "updated")
        self.assertNotIn("PermissionRequest", json.dumps(events))

    def test_ordinary_excel_write_runs_without_permission_request(self):
        if excel._openpyxl() is None:
            self.skipTest("openpyxl is not installed")
        target = self.workspace / "report.xlsx"
        request, execute, events, _ = self.run_call(
            "excel_write", {"path": str(target), "cells": {"A1": "updated"}},
        )
        request.assert_not_called()
        execute.assert_called_once()
        self.assertTrue(target.is_file())
        workbook = excel._openpyxl().load_workbook(target)
        try:
            self.assertEqual(workbook.active["A1"].value, "updated")
        finally:
            workbook.close()
        self.assertNotIn("PermissionRequest", json.dumps(events))

    def test_ordinary_edit_still_requests_approval(self):
        target = self.workspace / "report.txt"
        target.write_text("original", encoding="utf-8")
        request, execute, _, _ = self.run_call(
            "edit_file", {"path": str(target), "old_text": "original", "new_text": "updated"},
        )
        request.assert_called_once()
        execute.assert_called_once()
        self.assertEqual(target.read_text(encoding="utf-8"), "updated")


if __name__ == "__main__":
    unittest.main()
