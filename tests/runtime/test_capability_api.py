from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException


class CapabilityApiTests(unittest.TestCase):
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

    def test_save_preserves_removed_mcp_and_canonicalizes_aliases(self) -> None:
        from server.api.capabilities import CapabilitiesUpdate, put_assistant_capabilities
        from server.db import repos

        assistant = repos.create_template(
            name="兼容 MCP",
            capabilities={"mcp_servers": ["mcp:retired-server"]},
        )
        catalog = [
            {"id": "skill:skills/example", "kind": "skill", "name": "example"},
            {"id": "mcp:stable-server", "kind": "mcp", "name": "stable-server"},
        ]
        with patch("server.api.capabilities._visible_catalog", return_value=catalog), patch(
            "server.api.capabilities.load_merged_mcp_servers",
            return_value={
                "stable-server": {
                    "server_id": "stable-server",
                    "aliases": ["legacy-server"],
                }
            },
        ):
            result = put_assistant_capabilities(
                assistant["template_id"],
                CapabilitiesUpdate(
                    skills=["skill:skills/example"],
                    mcp_servers=["mcp:retired-server", "mcp:legacy-server"],
                ),
            )

        self.assertEqual(result["capabilities"]["skills"], ["skill:skills/example"])
        self.assertEqual(
            result["capabilities"]["mcp_servers"],
            ["mcp:stable-server", "mcp:retired-server"],
        )

    def test_save_rejects_new_unknown_mcp(self) -> None:
        from server.api.capabilities import CapabilitiesUpdate, put_assistant_capabilities
        from server.db import repos

        assistant = repos.create_template(name="MCP 校验")
        with patch("server.api.capabilities._visible_catalog", return_value=[]), patch(
            "server.api.capabilities.load_merged_mcp_servers", return_value={}
        ), self.assertRaises(HTTPException) as raised:
            put_assistant_capabilities(
                assistant["template_id"],
                CapabilitiesUpdate(mcp_servers=["mcp:unknown-server"]),
            )

        self.assertEqual(raised.exception.status_code, 400)
        self.assertEqual(raised.exception.detail["fields"], {"mcp_servers": ["mcp:unknown-server"]})
