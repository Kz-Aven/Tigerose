"""Stateful Claude/CodeBuddy-compatible Hook event normalization."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .recorder import ObservationRecorder
from .storage import data_root


def _value(event: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in event and event[name] is not None:
            return event[name]
    return ""


class HookLifecycleCollector:
    def __init__(self, recorder: ObservationRecorder | None = None) -> None:
        self.recorder = recorder or ObservationRecorder()

    def _path(self, platform: str, execution_key: str) -> Path:
        digest = hashlib.sha256(f"{platform}:{execution_key}".encode()).hexdigest()
        path = data_root() / "hook-state" / platform / f"{digest}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def _read(self, platform: str, execution_key: str) -> dict[str, Any] | None:
        path = self._path(platform, execution_key)
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def _write(self, platform: str, execution_key: str, value: dict[str, Any]) -> None:
        path = self._path(platform, execution_key)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)

    def _remove(self, platform: str, execution_key: str) -> None:
        self._path(platform, execution_key).unlink(missing_ok=True)

    @staticmethod
    def _assistant(event: dict[str, Any], platform: str) -> tuple[str, str]:
        workspace = str(_value(event, "workspace", "cwd", "working_directory") or "default")
        assistant_id = hashlib.sha256(workspace.encode()).hexdigest()[:16]
        default_name = {"workbuddy": "WorkBuddy", "codex": "Codex"}.get(platform, platform.title())
        return f"{platform}-{assistant_id}", str(_value(event, "agent_name", "assistant_name") or default_name)

    def ingest(self, event: dict[str, Any]) -> str | None:
        name = str(event.get("hook_event_name") or "")
        platform = str(event.get("source_platform") or event.get("platform") or "workbuddy").lower()
        session_id = str(_value(event, "session_id") or "unknown-session")
        turn_id = str(_value(event, "turn_id") or "session")
        key = f"{session_id}:{turn_id}"
        if name == "UserPromptSubmit":
            assistant_id, assistant_name = self._assistant(event, platform)
            self._write(platform, key, {"assistant_id": assistant_id, "assistant_name": assistant_name, "session_id": session_id, "user_input": _value(event, "prompt", "user_input", "input"), "spans": []})
            return None
        state = self._read(platform, key)
        if not state:
            return None
        if name == "PostToolUse":
            state["spans"].append({"key": str(_value(event, "tool_use_id", "tool_call_id", "tool_name", "tool") or len(state["spans"])), "type": "tool", "name": str(_value(event, "tool_name", "tool") or "tool"), "tool_name": str(_value(event, "tool_name", "tool") or "tool"), "input": _value(event, "tool_input", "input", "arguments"), "output": _value(event, "tool_response", "tool_output", "result", "result_summary"), "status": str(_value(event, "outcome", "status") or "success")})
            self._write(platform, key, state)
            return None
        if name not in {"Stop", "Interrupt", "SessionEnd"}:
            return None
        failed = name in {"Interrupt", "SessionEnd"}
        execution_id = self.recorder.record_execution(platform=platform, assistant_id=state["assistant_id"], assistant_name=state["assistant_name"], source_execution_id=key, session_id=session_id, user_input=state["user_input"], output=_value(event, "output", "final_output", "response"), status="failed" if failed else "completed", termination=name, collection_mode="hook_collector", coverage="partial", coverage_reason="WorkBuddy/CodeBuddy-compatible Hook payload omits verified model internals", trace_integrity="complete", spans=state["spans"])
        self._remove(platform, key)
        return execution_id
