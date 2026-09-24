from __future__ import annotations

import json
import sys
from pathlib import Path

from server.capabilities import hooks_config
from server.runtime.hooks import _payload, dispatch, execute_handler


def test_observe_command_accepts_empty_stdout():
    outcome = execute_handler(
        {"command": sys.executable, "args": ["-c", ""], "mode": "observe", "timeout_seconds": 2},
        {"event": "PostToolUse"},
    )
    assert outcome["execution"] == "succeeded"
    assert outcome["control"]["decision"] == "none"
    assert outcome["effects"] == [{"type": "command:completed"}]


def test_pre_tool_deny_short_circuits_and_honors_matcher():
    hook = {
        "hook_id": "deny-bash", "enabled": True, "event": "PreToolUse", "priority": 1,
        "matcher": {"tool.name": ["bash"]}, "failure_policy": "allow",
        "handler": {"command": sys.executable, "args": ["-c", 'print("{\\\"decision\\\": \\\"deny\\\", \\\"reason\\\": \\\"blocked\\\"}")'], "mode": "decision", "timeout_seconds": 2},
    }
    denied = dispatch([hook], "PreToolUse", {"tool": {"name": "bash"}})
    allowed = dispatch([hook], "PreToolUse", {"tool": {"name": "read_file"}})
    assert denied.decision == "deny"
    assert denied.reason == "blocked"
    assert allowed.decision == "none"


def test_project_hook_fully_overrides_same_global_id(tmp_path, monkeypatch):
    user_path = tmp_path / "hooks.json"
    project_path = tmp_path / "workspace" / ".agent" / "hooks.json"
    monkeypatch.setattr(hooks_config, "user_hooks_path", lambda: user_path)
    monkeypatch.setattr(hooks_config, "project_hooks_path", lambda _workspace: project_path)
    base = {
        "display_name": "Global", "event": "PostToolUse", "handler": {"type": "command", "command": sys.executable, "args": ["-c", ""], "mode": "observe"},
    }
    hooks_config.put_hook("shared-rule", base, "user")
    hooks_config.put_hook("shared-rule", {**base, "display_name": "Project", "enabled": False}, "project", tmp_path / "workspace")
    resolved = hooks_config.resolved_hooks(tmp_path / "workspace")
    assert len(resolved) == 1
    assert resolved[0]["display_name"] == "Project"
    assert resolved[0]["source"] == "project"


def test_first_run_seeds_editable_trafficlight_hooks(tmp_path, monkeypatch):
    path = tmp_path / "hooks.json"
    monkeypatch.setattr(hooks_config, "user_hooks_path", lambda: path)
    hooks_config.ensure_user_hooks_seeded()
    hooks = hooks_config.load_scope("user")
    assert len(hooks) == 6
    assert all(item["enabled"] for item in hooks)
    assert hooks_config.owns_trafficlight(hooks)
    assert {item["event"] for item in hooks} >= {"UserPromptSubmit", "PostToolUse", "RunFinished", "RunCancelled", "SessionEnd"}
    assert all(item["handler"]["trafficlight_agent_name"] == "Tigerose" for item in hooks)
    assert all(item["handler"]["trafficlight_keepalive"] is True for item in hooks)


def test_existing_official_trafficlight_hooks_are_migrated(tmp_path, monkeypatch):
    path = tmp_path / "hooks.json"
    monkeypatch.setattr(hooks_config, "user_hooks_path", lambda: path)
    path.write_text(json.dumps({"schema_version": 1, "hooks": [{
        "hook_id": "trafficlight-start", "handler": {"effect_kind": "trafficlight", "command": "/opt/homebrew/bin/trafficlight-codex-hook"},
    }]}), encoding="utf-8")
    hooks_config.ensure_user_hooks_seeded()
    migrated = json.loads(path.read_text(encoding="utf-8"))["hooks"][0]["handler"]
    assert migrated["trafficlight_agent_name"] == "Tigerose"
    assert migrated["trafficlight_keepalive"] is True


def test_custom_trafficlight_command_is_not_migrated(tmp_path, monkeypatch):
    path = tmp_path / "hooks.json"
    monkeypatch.setattr(hooks_config, "user_hooks_path", lambda: path)
    original = {"schema_version": 1, "hooks": [{
        "hook_id": "trafficlight-custom", "handler": {"effect_kind": "trafficlight", "command": "/usr/local/bin/custom-light"},
    }]}
    path.write_text(json.dumps(original), encoding="utf-8")
    hooks_config.ensure_user_hooks_seeded()
    assert json.loads(path.read_text(encoding="utf-8")) == original


def test_codex_payload_passes_trafficlight_identity_keepalive_and_run_ids():
    data = _payload(
        {"payload_profile": "codex-v1", "codex_event_name": "UserPromptSubmit", "trafficlight_agent_name": "Tigerose", "trafficlight_keepalive": True},
        {"event": "RunStarted", "run": {"session_id": "session-1", "run_id": "run-1"}},
    )
    assert data["hook_event_name"] == "UserPromptSubmit"
    assert data["session_id"] == "session-1"
    assert data["turn_id"] == "run-1"
    assert data["run"]["run_id"] == "run-1"
    assert data["trafficlight_agent_name"] == "Tigerose"
    assert data["trafficlight_keepalive"] is True
