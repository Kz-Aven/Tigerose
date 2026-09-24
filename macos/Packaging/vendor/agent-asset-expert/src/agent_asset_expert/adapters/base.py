"""Small, explicit platform adapter contract."""
from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ..storage import data_root


@dataclass(frozen=True)
class AdapterResult:
    platform: str
    detected: bool
    collection_mode: str
    coverage: str
    reason: str
    mcp_config: str = ""
    hook_config: str = ""


class Adapter:
    platform = "unknown"

    def detect(self) -> AdapterResult:
        raise NotImplementedError

    def install(self) -> AdapterResult:
        result = self.detect()
        if not result.detected:
            return result
        self._write_hook_config(result)
        return result

    def _write_hook_config(self, result: AdapterResult) -> None:
        root = data_root() / "hooks"
        root.mkdir(parents=True, exist_ok=True)
        (root / f"{self.platform}.json").write_text(json.dumps({"platform": self.platform, "collection_mode": result.collection_mode, "coverage": result.coverage, "collector": [sys.executable, "-m", "agent_asset_expert.collector"]}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class CodexAdapter(Adapter):
    platform = "codex"

    def detect(self) -> AdapterResult:
        home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
        config = home / "config.toml"
        if not config.is_file():
            return AdapterResult(self.platform, False, "log_collector", "partial", "Codex config.toml was not found")
        hooks = Path(os.environ.get("CODEX_HOOKS", home / "hooks.json"))
        reason = "Codex Hooks can collect lifecycle events; model internals require a future official runtime extension"
        if not hooks.is_file():
            reason = "Codex MCP config is available; Hooks were not found, so collection cannot start until hooks.json exists"
        return AdapterResult(self.platform, True, "hook_collector", "partial", reason, str(config), str(hooks) if hooks.is_file() else "")


class TigeroseAdapter(Adapter):
    platform = "tigerose"

    def detect(self) -> AdapterResult:
        home = Path(os.environ.get("TIGEROSE_HOME", Path.home() / "Library" / "Application Support" / "Tigerose"))
        config = home / "mcp.json"
        if not config.is_file():
            return AdapterResult(self.platform, False, "hook_collector", "partial", "Tigerose mcp.json was not found")
        root = os.environ.get("TIGEROSE_ROOT")
        if not root or not (Path(root) / "server" / "runtime" / "turn.py").is_file():
            return AdapterResult(self.platform, True, "hook_collector", "partial", "Tigerose MCP bridge is available; set TIGEROSE_ROOT for the optional full runtime adapter", str(config))
        return AdapterResult(self.platform, True, "runtime_adapter", "full", "Tigerose MCP bridge and runtime source are available", str(config))


class WorkBuddyAdapter(Adapter):
    platform = "workbuddy"

    def detect(self) -> AdapterResult:
        settings = Path(os.environ.get("WORKBUDDY_SETTINGS", Path.home() / ".workbuddy" / "settings.json"))
        runtime = Path(os.environ.get("WORKBUDDY_RUNTIME", "/Applications/WorkBuddy.app/Contents/Resources/app.asar.unpacked/cli/bin/codebuddy"))
        if not settings.is_file():
            return AdapterResult(self.platform, False, "hook_collector", "partial", "WorkBuddy settings.json was not found")
        reason = "WorkBuddy Hook lifecycle is available; bundled runtime internals are not patchable by default"
        if not runtime.is_file():
            reason = "WorkBuddy settings Hook lifecycle is available; bundled runtime path was not found"
        return AdapterResult(self.platform, True, "hook_collector", "partial", reason, str(Path.home() / ".workbuddy" / "mcp.json"), str(settings))


def adapters() -> dict[str, Adapter]:
    return {item.platform: item for item in (TigeroseAdapter(), CodexAdapter(), WorkBuddyAdapter())}
