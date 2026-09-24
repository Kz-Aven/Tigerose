from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException


class AssistantAvatarTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        home = Path(self.tmp.name) / "home"
        home.mkdir()
        self.env = patch.dict(os.environ, {"TIGEROSE_HOME": str(home)})
        self.env.start()
        self.addCleanup(self.env.stop)
        from avent_paths import reset_path_cache
        from server.db import schema

        reset_path_cache()
        self.addCleanup(reset_path_cache)
        schema.init_db()

    def test_avatar_id_persists_and_group_reads_latest_template_avatar(self) -> None:
        from server.db import repos

        assistant = repos.create_template(name="头像助理", avatar_id="human-avatars-01")
        group = repos.create_group(name="头像群")
        repos.add_member(group["group_id"], assistant["template_id"])

        self.assertEqual(
            repos.list_members(group["group_id"])[0]["avatar_id"],
            "human-avatars-01",
        )

        repos.update_template(assistant["template_id"], avatar_id="plant-avatars-02")
        self.assertEqual(
            repos.list_members(group["group_id"])[0]["avatar_id"],
            "plant-avatars-02",
        )

    def test_avatar_validator_rejects_unknown_resource(self) -> None:
        from server.api.assistants import validate_avatar_id

        self.assertEqual(validate_avatar_id("animal-avatars-01"), "animal-avatars-01")
        with self.assertRaises(HTTPException):
            validate_avatar_id("outside-the-catalog")

    def test_avatar_update_does_not_rewrite_stale_capabilities(self) -> None:
        from server.api.assistants import TemplateUpdate, patch_assistant
        from server.db import repos

        assistant = repos.create_template(
            name="旧配置助理",
            capabilities={"mcp_servers": ["mcp:removed-server"]},
        )

        updated = patch_assistant(
            assistant["template_id"],
            TemplateUpdate(avatar_id="animal-avatars-03"),
        )

        self.assertEqual(updated["avatar_id"], "animal-avatars-03")
        self.assertEqual(
            repos.get_template(assistant["template_id"])["capabilities"]["mcp_servers"],
            ["mcp:removed-server"],
        )


if __name__ == "__main__":
    unittest.main()
