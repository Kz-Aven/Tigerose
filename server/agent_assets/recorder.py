"""Failure-isolated Tigerose facade backed by Agent Asset Expert."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from agent_asset_expert.recorder import ObservationRecorder as AssetRecorder
from agent_asset_expert.storage import utc_now

log = logging.getLogger(__name__)

@dataclass
class _Execution:
    assistant_id: str
    assistant_name: str
    session_id: str
    surface: str
    user_input: Any
    started_at: str
    started_monotonic: float
    snapshots: list[dict[str, Any]] = field(default_factory=list)
    spans: list[dict[str, Any]] = field(default_factory=list)


class ObservationRecorder:
    def __init__(self) -> None:
        self._recorder = AssetRecorder()
        self._executions: dict[str, _Execution] = {}

    def _try(self, method: str, **kwargs: Any) -> Any:
        try:
            return getattr(self, f"_{method}")(**kwargs)
        except Exception:
            log.exception("agent asset collection failed: %s", method)
            return None

    def start_execution(self, **kwargs: Any) -> Any:
        return self._try("start_execution", **kwargs)

    def _start_execution(self, *, assistant_id: str, run_id: str, session_id: str, surface: str, user_input: Any) -> None:
        self._executions[run_id] = _Execution(assistant_id, assistant_id, session_id, surface, user_input, utc_now(), time.monotonic())

    def finish_execution(self, **kwargs: Any) -> Any:
        return self._try("finish_execution", **kwargs)

    def _finish_execution(self, *, assistant_id: str, run_id: str, status: str, termination: str, output: Any = "", error: Exception | None = None) -> str | None:
        execution = self._executions.pop(run_id, None)
        if not execution:
            return None
        ended_at = utc_now()
        execution_id = self._recorder.record_execution(
            platform="tigerose", assistant_id=execution.assistant_id, assistant_name=execution.assistant_name,
            source_execution_id=run_id, session_id=execution.session_id, user_input=execution.user_input,
            output=output, status=status, termination=termination, collection_mode="runtime_adapter",
            coverage="full", trace_integrity="complete", spans=execution.spans,
            started_at=execution.started_at, ended_at=ended_at,
            error_code="SYSTEM_ERROR" if error else None,
            error_message=str(error) if error else None,
        )
        if not execution_id:
            return None
        for snapshot in execution.snapshots:
            self._recorder.store.snapshot(execution_id, snapshot["asset_type"], snapshot["asset_id"], snapshot["value"], snapshot["purpose"])
        return execution_id

    def record_llm(self, **kwargs: Any) -> Any:
        return self._try("record_llm", **kwargs)

    def _record_llm(self, *, run_id: str, call_id: str, call_kind: str, profile: dict[str, Any], request: Any, response: Any = None, usage: dict[str, Any] | None = None, error: Exception | None = None, started_at: float | None = None, **_: Any) -> None:
        execution = self._executions.get(run_id)
        if not execution:
            return
        usage = usage or {}
        duration_ms = max(0, int((time.time() - (started_at or time.time())) * 1000))
        execution.spans.append({"key": call_id, "type": "llm", "name": call_kind, "model_id": str(profile.get("id") or request.get("model") or ""), "input": request, "output": response, "status": "error" if error else "success", "input_tokens": int(usage.get("input_tokens") or 0), "cached_input_tokens": int(usage.get("cached_input_tokens") or 0), "output_tokens": int(usage.get("output_tokens") or 0), "total_tokens": int(usage.get("total_tokens") or 0), "duration_ms": duration_ms, "error_code": "LLM_ERROR" if error else None, "error_message": str(error) if error else None, "metadata": {"usage": usage}})

    def record_tool(self, **kwargs: Any) -> Any:
        return self._try("record_tool", **kwargs)

    def _record_tool(self, *, run_id: str, call_id: str, name: str, args: Any, result: Any, outcome: str, metadata: Any = None, **_: Any) -> None:
        execution = self._executions.get(run_id)
        if execution:
            execution.spans.append({"key": call_id, "type": "tool", "name": name, "tool_name": name, "input": args, "output": result, "status": "success" if outcome == "ok" else outcome, "error_code": None if outcome == "ok" else "TOOL_ERROR", "error_message": None if outcome == "ok" else str(result), "metadata": metadata or {}})

    def snapshot(self, **kwargs: Any) -> Any:
        return self._try("snapshot", **kwargs)

    def _snapshot(self, *, run_id: str, asset_type: str, asset_id: str, value: Any, purpose: str, **_: Any) -> None:
        execution = self._executions.get(run_id)
        if execution:
            if asset_type == "agent" and isinstance(value, dict):
                execution.assistant_name = str(value.get("name") or execution.assistant_name)
            execution.snapshots.append({"asset_type": asset_type, "asset_id": asset_id, "value": value, "purpose": purpose})

    def feedback(self, **kwargs: Any) -> Any:
        return self._try("feedback", **kwargs)

    def _feedback(self, *, execution_id: str, feedback_type: str, reason: str = "", content: Any = None, **_: Any) -> str | None:
        return self._recorder.store.feedback(execution_id, feedback_type, reason, content)

recorder = ObservationRecorder()
