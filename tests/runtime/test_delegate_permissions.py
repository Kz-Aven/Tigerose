from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from server.runtime.context import TurnContext
from server.runtime.tools.delegate import run_subagent


class DelegatePermissionTests(unittest.TestCase):
    def test_subagent_inherits_parent_permission_channel_and_policy(self):
        ctx = TurnContext(
            template_id="tpl_1",
            scope_key="user:local:assistant:tpl_1",
            session_id="session_1",
            cwd=Path.cwd(),
            surface="assistant",
        )
        with (
            patch("server.db.repos.get_template", return_value={
                "config_meta": {"file_access": "ask"}
            }),
            patch("server.runtime.tools.handlers.build_handlers", return_value=({}, frozenset())),
            patch("server.runtime.loop.run_tool_loop", return_value={
                "reply": "done",
                "termination": "normal_stop",
                "rounds": 1,
            }) as run_loop,
        ):
            run_subagent(ctx=ctx, client=object(), model="test", prompt="edit /tmp/file.txt")

        kwargs = run_loop.call_args.kwargs
        self.assertEqual(kwargs["permission_channel"], "assistant:tpl_1")
        self.assertEqual(kwargs["permission_policy"].file_access, "ask")
        self.assertIn("the user can approve access", kwargs["messages"][0]["content"])


if __name__ == "__main__":
    unittest.main()
