"""Platform-neutral event contract."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

CollectionMode = Literal["runtime_adapter", "hook_collector", "log_collector"]
Coverage = Literal["full", "partial"]
TraceIntegrity = Literal["complete", "incomplete"]


@dataclass(frozen=True)
class SourceIdentity:
    platform: str
    assistant_id: str
    assistant_name: str
    source_execution_id: str
    session_id: str = ""


@dataclass(frozen=True)
class CanonicalExecutionEvent:
    event_type: str
    identity: SourceIdentity
    payload: dict[str, Any] = field(default_factory=dict)
    source_event_type: str = ""
    collection_mode: CollectionMode = "runtime_adapter"
    coverage: Coverage = "full"
