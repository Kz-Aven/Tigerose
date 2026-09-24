from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from server.db import mesh_repos as mesh, repos, schema
from server.runtime.context import TurnContext
from server.runtime.tools.mesh import handlers


class MeshArtifactToolsTests(unittest.TestCase):
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
        self.a, self.b, self.c, self.d = [repos.create_template(name=name)["template_id"] for name in "ABCD"]
        mesh.configure(self.a, [self.b])
        mesh.configure(self.b, [self.c, self.d])
        self.parent = mesh.delegate(self.a, self.b, "parent", ["done"], idempotency_key="parent")
        root = Path(mesh.task_workspace(self.parent["task_id"]))
        root.mkdir(parents=True)
        self.source = root / "evidence.txt"
        self.source.write_text("verified evidence", encoding="utf-8")
        self.artifact = mesh.register_artifact(self.parent["task_id"], self.b, str(self.source), name="../../证据.txt")
        self.child = mesh.delegate(self.b, self.c, "analyze artifact", ["done"],
                                   parent_task_id=self.parent["task_id"], artifact_refs=[self.artifact["artifact_id"]], idempotency_key="child")

    def read(self, task, actor, artifact_id=None):
        ctx = TurnContext(template_id=actor, surface="mesh", mesh_task_id=task["task_id"],
                          scope_key=f"mesh:{task['task_id']}:{actor}", session_id="artifact-test",
                          cwd=Path(mesh.task_workspace(task["task_id"])))
        return handlers(ctx)["read_task_artifact"](artifact_id=artifact_id or self.artifact["artifact_id"])

    def test_forwarded_file_is_readable_inside_receiver_workspace(self):
        result = self.read(self.child, self.c)
        self.assertEqual(result.outcome, "ok")
        payload = json.loads(result.content)
        path = Path(payload["path"])
        self.assertTrue(path.is_relative_to(Path(mesh.task_workspace(self.child["task_id"])) / "inputs"))
        self.assertEqual(path.read_text(), "verified evidence")
        self.assertEqual(payload["name"], "证据.txt")
        self.assertNotIn(str(self.source), result.content)
        self.assertEqual(json.loads(self.read(self.child, self.c).content)["path"], str(path))

    def test_unrelated_task_and_ungranted_participant_are_rejected(self):
        other = mesh.delegate(self.b, self.d, "unrelated", ["done"], parent_task_id=self.parent["task_id"], idempotency_key="other")
        for actor in (self.b, self.d):
            result = self.read(other, actor)
            self.assertEqual(result.outcome, "error")
            self.assertEqual(json.loads(result.content)["error"], "permission_denied")
        self.assertFalse((Path(mesh.task_workspace(other["task_id"])) / "inputs").exists())

    def test_inputs_symlink_cannot_write_outside_workspace(self):
        root = Path(mesh.task_workspace(self.child["task_id"]))
        root.mkdir(parents=True)
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        (root / "inputs").symlink_to(outside, target_is_directory=True)
        result = self.read(self.child, self.c)
        self.assertEqual(result.outcome, "error")
        self.assertEqual(json.loads(result.content)["error"], "permission_denied")
        self.assertEqual(list(outside.iterdir()), [])

    def test_mutated_source_is_not_materialized(self):
        self.source.write_text("changed after publication")
        result = self.read(self.child, self.c)
        self.assertEqual(result.outcome, "error")
        self.assertEqual(json.loads(result.content)["error"], "stale_revision")
        self.assertFalse((Path(mesh.task_workspace(self.child["task_id"])) / "inputs").exists())

    def test_tool_is_registered_without_client_control_of_identity(self):
        from server.runtime.tools.registry import TOOL_SPECS
        spec = TOOL_SPECS["read_task_artifact"]
        self.assertEqual(set(spec["parameters"]["properties"]), {"artifact_id"})


if __name__ == "__main__":
    unittest.main()
