"""Seed preset agent templates on first boot; backfill model + capabilities."""

from __future__ import annotations

import json
from pathlib import Path

from server.db import repos
from server.presets.default_capabilities import defaults_for_role, is_empty_capabilities
from server.runtime.turn import default_model_profile_id

PRESET_PATH = Path(__file__).resolve().parent / "presets" / "agents.json"


def seed_presets_if_empty() -> None:
    if not repos.list_templates():
        data = json.loads(PRESET_PATH.read_text(encoding="utf-8"))
        default_model = default_model_profile_id()
        for item in data:
            role = item.get("role", "")
            caps = item.get("capabilities") or defaults_for_role(role, item.get("name", ""))
            repos.create_template(
                name=item["name"],
                role=role,
                system_prompt=item.get("system_prompt", ""),
                theme_color=item.get("theme_color", "#5db8a6"),
                tools_allowlist=caps.get("tools") or item.get("tools_allowlist", []),
                capabilities=caps,
                model_profile_id=item.get("model_profile_id") or default_model,
            )
    backfill_missing_models()
    backfill_empty_capabilities()


def backfill_missing_models() -> None:
    default_model = default_model_profile_id()
    for tpl in repos.list_templates():
        if not (tpl.get("model_profile_id") or "").strip():
            repos.update_template(tpl["template_id"], model_profile_id=default_model)


def backfill_empty_capabilities() -> None:
    """Existing installs seeded with empty allowlists — enable role defaults once."""
    prompt_hints = {
        "Code": (
            "You are a software engineer with full local tools "
            "(read_file, write_file, edit_file, glob, bash, task board, etc.). "
            "When the user asks about files or code, call tools instead of claiming "
            "you cannot access the filesystem. Propose concrete changes, keep diffs "
            "focused, and explain risks briefly."
        ),
    }
    for tpl in repos.list_templates():
        caps = tpl.get("capabilities") or {}
        if not is_empty_capabilities(caps):
            continue
        defaults = defaults_for_role(tpl.get("role") or "", tpl.get("name") or "")
        fields: dict = {"capabilities": defaults}
        role = tpl.get("role") or tpl.get("name") or ""
        if role in prompt_hints:
            fields["system_prompt"] = prompt_hints[role]
        repos.update_template(tpl["template_id"], **fields)
