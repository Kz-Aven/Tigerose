"""Append-only run transcripts that survive session clear/delete."""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from avent_paths import data_root, ensure_data_dirs

_lock = threading.Lock()
_SCHEMA_VERSION = 1
_MAX_EVENT_BYTES = 512 * 1024
_SENSITIVE_KEY = re.compile(
    r"(?:api[_-]?key|authorization|cookie|password|passwd|secret|token|session[_-]?id|credential)",
    re.IGNORECASE,
)
_AUTH_VALUE = re.compile(r"\b(?:bearer|basic|token)\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE)


def redact_sensitive_data(value: Any, *, key: str = "") -> Any:
    """Return JSON-safe audit data without credentials or authorization values."""
    if key and _SENSITIVE_KEY.search(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(k): redact_sensitive_data(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [redact_sensitive_data(item) for item in value]
    if isinstance(value, str):
        return _AUTH_VALUE.sub("[REDACTED]", value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)


def _bounded_payload(payload: dict[str, Any]) -> dict[str, Any]:
    encoded = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    if len(encoded) <= _MAX_EVENT_BYTES:
        return payload
    bounded = dict(payload)
    for field in ("result", "content", "detail", "metadata", "args"):
        if field not in bounded:
            continue
        bounded[field] = {
            "truncated": True,
            "original_bytes": len(encoded),
            "preview": str(bounded[field])[:4000],
        }
        encoded = json.dumps(bounded, ensure_ascii=False, default=str).encode("utf-8")
        if len(encoded) <= _MAX_EVENT_BYTES:
            break
    if len(encoded) > _MAX_EVENT_BYTES:
        bounded = {
            key: value
            for key, value in bounded.items()
            if key
            in {
                "schema_version",
                "event_id",
                "ts",
                "run_id",
                "kind",
                "stage",
                "tool",
                "tool_call_id",
                "outcome",
            }
        }
    bounded["payload_truncated"] = True
    bounded["payload_limit_bytes"] = _MAX_EVENT_BYTES
    return bounded


def runs_root() -> Path:
    ensure_data_dirs()
    root = data_root() / "runs"
    root.mkdir(parents=True, exist_ok=True)
    return root


def transcript_path(run_id: str) -> Path:
    safe = "".join(c for c in run_id if c.isalnum() or c in {"_", "-"})
    return runs_root() / f"{safe}.jsonl"


def append_run_event(run_id: str, event: dict[str, Any]) -> None:
    if not run_id:
        return
    payload = redact_sensitive_data(dict(event))
    if not isinstance(payload, dict):
        return
    payload.setdefault("schema_version", _SCHEMA_VERSION)
    payload.setdefault("event_id", f"evt_{uuid.uuid4().hex}")
    payload.setdefault("ts", time.time())
    payload.setdefault("run_id", run_id)
    payload = _bounded_payload(payload)
    line = json.dumps(payload, ensure_ascii=False, default=str)
    path = transcript_path(run_id)
    with _lock:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")


def read_run_events(run_id: str, *, limit: int = 5000) -> list[dict[str, Any]]:
    path = transcript_path(run_id)
    if not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
            if len(out) >= limit:
                break
    return out
