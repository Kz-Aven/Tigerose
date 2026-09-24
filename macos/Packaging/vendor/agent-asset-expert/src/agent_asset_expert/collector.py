"""NDJSON hook collector for platforms that cannot load a runtime adapter."""
from __future__ import annotations

import json
import os
import sys
import uuid
from typing import Any

from .recorder import ObservationRecorder
from .hook_lifecycle import HookLifecycleCollector


def ingest(event: dict[str, Any]) -> str | None:
    """Record a completed execution supplied by a platform Hook.

    The caller owns event delivery. Malformed or unsupported events are ignored so a
    Hook never changes the host Agent outcome.
    """
    if event.get("hook_event_name"):
        if not event.get("source_platform") and os.environ.get("AGENT_ASSET_EXPERT_PLATFORM"):
            event = {**event, "source_platform": os.environ["AGENT_ASSET_EXPERT_PLATFORM"]}
        return HookLifecycleCollector().ingest(event)
    if event.get("event_type") not in ("execution.finished", "execution.failed"):
        return None
    platform = str(event.get("source_platform") or "")
    assistant_id = str(event.get("assistant_id") or "")
    if not platform or not assistant_id:
        return None
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    return ObservationRecorder().record_execution(
        platform=platform,
        assistant_id=assistant_id,
        assistant_name=str(event.get("assistant_name") or assistant_id),
        source_execution_id=str(event.get("source_execution_id") or uuid.uuid4()),
        session_id=str(event.get("session_id") or ""),
        user_input=payload.get("user_input", ""),
        output=payload.get("output", ""),
        status=str(payload.get("status") or ("failed" if event["event_type"] == "execution.failed" else "completed")),
        termination=str(payload.get("termination") or event["event_type"]),
        collection_mode="hook_collector",
        coverage="partial",
        coverage_reason="Hook event payload; runtime internals were not verified",
        trace_integrity="complete",
        spans=payload.get("spans") if isinstance(payload.get("spans"), list) else [],
    )


def main() -> None:
    raw = sys.stdin.read().strip()
    if not raw:
        return
    try:
        value = json.loads(raw)
        if isinstance(value, dict):
            ingest(value)
            return
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    for line in raw.splitlines():
        try:
            ingest(json.loads(line))
        except (json.JSONDecodeError, TypeError, ValueError):
            continue


if __name__ == "__main__":
    main()
