from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from server.capabilities.catalog import format_skill_context, load_skill_texts
from server.runtime.context import TurnContext
from server.runtime.loop import build_tool_surface
from server.runtime.tools.session_ops import get_skill
from server.runtime.turn import _build_system_prompt


class SkillContextTests(unittest.TestCase):
    def test_paths_survive_prompt_and_get_skill_with_project_precedence(self):
        with tempfile.TemporaryDirectory(prefix="skill paths ") as directory:
            root = Path(directory)
            workspace = root / "workspace"
            user_root = root / "user skills"
            project_skill = workspace / ".agent/skills/image-test/SKILL.md"
            user_skill = user_root / "image-test/SKILL.md"
            for path, content in ((project_skill, "project instructions"), (user_skill, "user instructions")):
                path.parent.mkdir(parents=True)
                path.write_text(content, encoding="utf-8")
            ctx = TurnContext(
                template_id="test", scope_key="test", session_id="test",
                cwd=workspace, surface="assistant_dm",
                enabled_skill_ids=["skill:skills/image-test"],
            )
            with patch("server.capabilities.catalog.user_skills_dir", return_value=user_root):
                loaded = load_skill_texts(ctx.enabled_skill_ids, workspace=workspace)
                result = get_skill(ctx, "image-test")
                prompt = _build_system_prompt({"name": "test"}, skill_texts=loaded)
            for text in (result, prompt):
                self.assertIn(str(project_skill.resolve()), text)
                self.assertIn(f"Skill resource directory: {project_skill.resolve().parent}", text)
                self.assertIn("project instructions", text)
                self.assertNotIn("user instructions", text)
            self.assertIn("not the workspace", result)
            self.assertIn("Quote paths containing spaces", result)
            self.assertIn("not found", get_skill(ctx, "disabled-skill"))

    def test_tool_surface_grants_only_resolved_enabled_skill_roots(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "workspace"
            skill = workspace / ".agent/skills/enabled/SKILL.md"
            disabled = workspace / ".agent/skills/disabled/SKILL.md"
            for path in (skill, disabled):
                path.parent.mkdir(parents=True)
                path.write_text("instructions", encoding="utf-8")
            ctx = TurnContext(
                template_id="test", scope_key="test", session_id="test",
                cwd=workspace, surface="assistant_dm",
            )
            surface = build_tool_surface(
                bundle={"skills": [{"id": "skill:skills/enabled"}, {"id": "skill:skills/missing", "missing": True}]},
                ctx=ctx,
            )
            self.assertEqual(surface["executor"].skill_roots, (skill.resolve().parent,))

    def test_mesh_surface_hides_legacy_teammate_spawning(self):
        with tempfile.TemporaryDirectory() as directory:
            ctx = TurnContext(
                template_id="test", scope_key="test", session_id="test",
                cwd=Path(directory), surface="assistant_dm",
            )
            with patch("server.runtime.feature_flags.flag_enabled", return_value=True), \
                 patch("server.runtime.loop.connect_mcp_servers", return_value={"clients": {}, "lookup": {}, "warnings": []}), \
                 patch("server.runtime.loop.build_handlers", return_value=({}, set())):
                surface = build_tool_surface(
                    bundle={"tools": [{"name": "spawn_teammate"}]},
                    ctx=ctx,
                )
            self.assertIn("delegate_agent", surface["tool_names"])
            self.assertNotIn("spawn_teammate", surface["tool_names"])

    def test_path_header_is_retained_for_long_skill_content(self):
        result = format_skill_context({"id": "test", "path": "/tmp/skill/SKILL.md", "content": "x" * 12000})
        self.assertIn("Skill resource directory:", result)
        self.assertTrue(result.endswith("x" * 12000))


if __name__ == "__main__":
    unittest.main()
