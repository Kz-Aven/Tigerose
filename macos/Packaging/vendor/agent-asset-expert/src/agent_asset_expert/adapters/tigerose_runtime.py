"""Runtime-facing Tigerose bridge.

Tigerose calls this bridge at run start, each model/tool completion, and run
finish. The bridge owns no Tigerose imports, so it is safe to ship separately.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..recorder import ObservationRecorder


@dataclass
class _Turn:
    assistant_id: str
    assistant_name: str
    session_id: str
    user_input: Any
    snapshots: list[dict[str, Any]] = field(default_factory=list)
    spans: list[dict[str, Any]] = field(default_factory=list)


class TigeroseRuntimeBridge:
    def __init__(self, recorder: ObservationRecorder | None = None) -> None:
        self.recorder = recorder or ObservationRecorder()
        self._turns: dict[str, _Turn] = {}

    def start(self, run_id: str, assistant_id: str, assistant_name: str, session_id: str, user_input: Any, snapshots: list[dict[str, Any]] | None = None) -> None:
        self._turns[run_id] = _Turn(assistant_id, assistant_name, session_id, user_input, snapshots or [])

    def llm_finished(self, run_id: str, call_id: str, model_id: str, request: Any, response: Any, usage: dict[str, Any] | None = None, error: str = "") -> None:
        turn = self._turns.get(run_id)
        if turn:
            turn.spans.append({"key": call_id, "type": "llm", "name": "completion", "model_id": model_id, "input": request, "output": response, "status": "error" if error else "success", "metadata": {"usage": usage or {}, "error": error}})

    def tool_finished(self, run_id: str, call_id: str, name: str, arguments: Any, result: Any, outcome: str = "success") -> None:
        turn = self._turns.get(run_id)
        if turn:
            turn.spans.append({"key": call_id, "type": "tool", "name": name, "tool_name": name, "input": arguments, "output": result, "status": outcome})

    def finish(self, run_id: str, output: Any, status: str, termination: str, integrity_reason: str = "") -> str | None:
        turn = self._turns.pop(run_id, None)
        if not turn:
            return None
        execution_id = self.recorder.record_execution(platform="tigerose", assistant_id=turn.assistant_id, assistant_name=turn.assistant_name, source_execution_id=run_id, session_id=turn.session_id, user_input=turn.user_input, output=output, status=status, termination=termination, collection_mode="runtime_adapter", coverage="full", trace_integrity="incomplete" if integrity_reason else "complete", integrity_reason=integrity_reason, spans=turn.spans)
        if execution_id:
            for snapshot in turn.snapshots:
                self.recorder.store.snapshot(execution_id, str(snapshot["asset_type"]), str(snapshot["asset_id"]), snapshot.get("value", {}), str(snapshot.get("purpose", "runtime")))
        return execution_id
