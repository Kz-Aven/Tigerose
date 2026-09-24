from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from server.runtime import permissions
from server.runtime.policy import PermissionPolicy
from server.runtime.scope_guard import build_scope_for_intent, classify_scope
from server.runtime.tools import files
from server.runtime.trusted_paths import configured_trusted_roots


class TrustedDataRootTests(unittest.TestCase):
    def setUp(self):
        self.workspace_tmp = tempfile.TemporaryDirectory()
        self.data_tmp = tempfile.TemporaryDirectory()
        self.external_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.workspace_tmp.cleanup)
        self.addCleanup(self.data_tmp.cleanup)
        self.addCleanup(self.external_tmp.cleanup)
        self.workspace = Path(self.workspace_tmp.name)
        self.data_root = Path(self.data_tmp.name)
        self.external_root = Path(self.external_tmp.name)

    def test_configured_roots_only_accept_absolute_strings(self):
        roots = configured_trusted_roots({
            "agent": {
                "trusted_data_roots": [
                    str(self.data_root),
                    str(self.data_root),
                    "relative/path",
                    12,
                ]
            }
        })
        self.assertEqual(roots, (self.data_root.resolve(),))

    def test_trusted_root_bypasses_workspace_permission_and_file_guard(self):
        trusted_file = self.data_root / "settings.txt"
        trusted_file.write_text("before", encoding="utf-8")
        roots = (self.data_root,)
        policy = PermissionPolicy(file_access="workspace")

        for name in ("read_file", "write_file", "edit_file"):
            with self.subTest(tool=name):
                self.assertIsNone(permissions.classify_permission(
                    name,
                    {"path": str(trusted_file)},
                    cwd=self.workspace,
                    policy=policy,
                    trusted_roots=roots,
                ))
        self.assertIsNone(permissions.classify_permission(
            "bash",
            {"command": f"cat {trusted_file}"},
            cwd=self.workspace,
            policy=policy,
            trusted_roots=roots,
        ))

        files.edit_file(
            self.workspace,
            str(trusted_file),
            "before",
            "after",
            trusted_roots=roots,
        )
        self.assertEqual(trusted_file.read_text(encoding="utf-8"), "after")

    def test_untrusted_external_root_stays_denied(self):
        external_file = self.external_root / "private.txt"
        self.assertEqual(
            permissions.classify_permission(
                "read_file",
                {"path": str(external_file)},
                cwd=self.workspace,
                policy=PermissionPolicy(file_access="workspace"),
                trusted_roots=(self.data_root,),
            )["action"],
            "deny",
        )
        with self.assertRaises(PermissionError):
            files.ensure_under(self.workspace, external_file, (self.data_root,))

    def test_trusted_edit_skips_the_scope_edit_confirmation(self):
        gate = classify_scope(
            build_scope_for_intent("one_shot_action"),
            "edit_file",
            {"path": str(self.data_root / "config.yaml")},
            cwd=str(self.workspace),
            trusted_roots=(self.data_root,),
        )
        self.assertIsNone(gate)


if __name__ == "__main__":
    unittest.main()
